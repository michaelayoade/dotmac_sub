"""Bulk device operations with optional all-or-nothing semantics.

This module provides a wrapper for executing device operations on multiple
targets, with support for:
- All-or-nothing: Rollback entire batch on any failure
- Best-effort: Continue on failures, report partial results
- Savepoint support for proper transaction boundaries
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.services.network.device_operation import (
    DeviceOperationContext,
    DeviceOperationResult,
    DeviceOperationStep,
)

logger = logging.getLogger(__name__)


@dataclass
class BulkOperationResult:
    """Result of a bulk device operation."""

    total: int
    succeeded: int
    failed: int
    results: list[DeviceOperationResult] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        """True if all operations succeeded."""
        return self.failed == 0 and self.succeeded == self.total

    @property
    def partial_success(self) -> bool:
        """True if some but not all operations succeeded."""
        return self.succeeded > 0 and self.failed > 0


@dataclass
class BulkOperationTarget:
    """A single target for bulk operations."""

    target_id: str
    steps: list[DeviceOperationStep]
    label: str = ""


class BulkDeviceOperation:
    """Execute operations on multiple targets with optional all-or-nothing.

    Usage:
        bulk = BulkDeviceOperation(db, all_or_nothing=True)
        bulk.add_target("ont-1", [step1, step2])
        bulk.add_target("ont-2", [step1, step2])
        result = bulk.execute()

    With all_or_nothing=True:
    - Uses db.begin_nested() for savepoint support
    - Rolls back entire batch on first failure
    - Returns immediately on failure

    With all_or_nothing=False:
    - Continues processing on failure
    - Reports partial results
    - Each target has independent success/failure
    """

    def __init__(
        self,
        db: Session,
        *,
        all_or_nothing: bool = True,
        operation_type: str = "bulk_operation",
        initiated_by: str | None = None,
    ):
        self.db = db
        self.all_or_nothing = all_or_nothing
        self.operation_type = operation_type
        self.initiated_by = initiated_by
        self.targets: list[BulkOperationTarget] = []

    def add_target(
        self,
        target_id: str,
        steps: list[DeviceOperationStep],
        label: str = "",
    ) -> None:
        """Add a target with its operation steps."""
        self.targets.append(BulkOperationTarget(
            target_id=target_id,
            steps=steps,
            label=label or target_id,
        ))

    def execute(self) -> BulkOperationResult:
        """Execute operations on all targets.

        Returns:
            BulkOperationResult with aggregated results.
        """
        if not self.targets:
            return BulkOperationResult(
                total=0,
                succeeded=0,
                failed=0,
                results=[],
            )

        results: list[DeviceOperationResult] = []
        succeeded = 0
        failed = 0

        if self.all_or_nothing:
            return self._execute_all_or_nothing(results)

        return self._execute_best_effort(results)

    def _execute_all_or_nothing(
        self,
        results: list[DeviceOperationResult],
    ) -> BulkOperationResult:
        """Execute with all-or-nothing semantics using savepoints."""
        succeeded = 0
        failed = 0

        try:
            # Create a savepoint for the entire batch
            with self.db.begin_nested():
                for target in self.targets:
                    ctx = DeviceOperationContext(
                        self.db,
                        self.operation_type,
                        target.target_id,
                        all_or_nothing=True,
                        initiated_by=self.initiated_by,
                    )
                    for step in target.steps:
                        ctx.add_step(step)

                    result = ctx.execute()
                    results.append(result)

                    if result.success:
                        succeeded += 1
                        logger.info(
                            "Bulk operation target %s succeeded",
                            target.label,
                        )
                    else:
                        failed += 1
                        logger.warning(
                            "Bulk operation target %s failed: %s",
                            target.label,
                            result.message,
                        )
                        # Savepoint will be rolled back automatically
                        raise _BulkOperationFailure(
                            f"Target {target.label} failed: {result.message}"
                        )

        except _BulkOperationFailure as exc:
            # All changes rolled back via savepoint
            logger.warning("Bulk operation rolled back: %s", exc)
            # Mark remaining targets as not executed
            remaining = len(self.targets) - len(results)
            for _ in range(remaining):
                results.append(DeviceOperationResult(
                    success=False,
                    message="Skipped due to earlier failure (all-or-nothing mode)",
                ))
                failed += 1

        return BulkOperationResult(
            total=len(self.targets),
            succeeded=succeeded,
            failed=failed,
            results=results,
        )

    def _execute_best_effort(
        self,
        results: list[DeviceOperationResult],
    ) -> BulkOperationResult:
        """Execute best-effort, continuing on failures."""
        succeeded = 0
        failed = 0

        for target in self.targets:
            try:
                ctx = DeviceOperationContext(
                    self.db,
                    self.operation_type,
                    target.target_id,
                    all_or_nothing=False,  # Individual target can have partial success
                    initiated_by=self.initiated_by,
                )
                for step in target.steps:
                    ctx.add_step(step)

                result = ctx.execute()
                results.append(result)

                if result.success:
                    succeeded += 1
                    logger.info(
                        "Bulk operation target %s succeeded",
                        target.label,
                    )
                else:
                    failed += 1
                    logger.warning(
                        "Bulk operation target %s failed: %s",
                        target.label,
                        result.message,
                    )

            except Exception as exc:
                failed += 1
                logger.error(
                    "Bulk operation target %s error: %s",
                    target.label,
                    exc,
                    exc_info=True,
                )
                results.append(DeviceOperationResult(
                    success=False,
                    message=f"Unexpected error: {exc}",
                ))

        return BulkOperationResult(
            total=len(self.targets),
            succeeded=succeeded,
            failed=failed,
            results=results,
        )


class _BulkOperationFailure(Exception):
    """Internal exception for bulk operation failures."""

    pass
