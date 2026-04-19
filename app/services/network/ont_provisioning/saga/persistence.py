"""Saga execution persistence for tracking and observability.

This module provides the SagaExecutionRepository for:
- Creating saga execution records
- Updating status during execution
- Marking completion (success or failure)
- Querying execution history
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.saga_execution import SagaExecution, SagaExecutionStatus
from app.services.network.ont_provisioning.saga.types import (
    SagaContext,
    SagaDefinition,
    SagaResult,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SagaExecutionRepository:
    """Repository for saga execution persistence.

    Provides CRUD operations for saga execution tracking with
    proper status transitions and history management.
    """

    def create(
        self,
        db: Session,
        saga: SagaDefinition,
        context: SagaContext,
    ) -> SagaExecution:
        """Create a new saga execution record.

        Args:
            db: Database session.
            saga: The saga definition being executed.
            context: Execution context with input data.

        Returns:
            The created SagaExecution record.
        """
        ont_unit_id = None
        olt_device_id = None

        # Try to parse UUIDs if provided
        try:
            ont_unit_id = UUID(context.ont_id) if context.ont_id else None
        except (ValueError, TypeError):
            pass

        if context.olt is not None:
            olt_device_id = context.olt.id

        execution = SagaExecution(
            id=UUID(context.saga_execution_id),
            saga_name=saga.name,
            saga_version=saga.version,
            ont_unit_id=ont_unit_id,
            olt_device_id=olt_device_id,
            status=SagaExecutionStatus.pending,
            input_data={
                "step_data": context.step_data,
                "dry_run": context.dry_run,
            },
            initiated_by=context.initiated_by,
        )

        db.add(execution)
        db.flush()

        logger.debug(
            "Created saga execution record: %s",
            execution.id,
            extra={
                "event": "saga_execution_created",
                "saga_name": saga.name,
                "saga_execution_id": str(execution.id),
            },
        )

        return execution

    def mark_running(self, db: Session, execution_id: str | UUID) -> SagaExecution | None:
        """Mark a saga execution as running.

        Args:
            db: Database session.
            execution_id: Saga execution ID.

        Returns:
            Updated SagaExecution or None if not found.
        """
        if isinstance(execution_id, str):
            execution_id = UUID(execution_id)

        execution = db.get(SagaExecution, execution_id)
        if execution is None:
            return None

        execution.status = SagaExecutionStatus.running
        execution.started_at = datetime.now(UTC)
        db.flush()

        return execution

    def mark_compensating(
        self,
        db: Session,
        execution_id: str | UUID,
    ) -> SagaExecution | None:
        """Mark a saga execution as compensating.

        Args:
            db: Database session.
            execution_id: Saga execution ID.

        Returns:
            Updated SagaExecution or None if not found.
        """
        if isinstance(execution_id, str):
            execution_id = UUID(execution_id)

        execution = db.get(SagaExecution, execution_id)
        if execution is None:
            return None

        execution.status = SagaExecutionStatus.compensating
        db.flush()

        return execution

    def mark_completed(
        self,
        db: Session,
        execution_id: str | UUID,
        result: SagaResult,
    ) -> SagaExecution | None:
        """Mark a saga execution as completed (success or failure).

        Args:
            db: Database session.
            execution_id: Saga execution ID.
            result: The saga execution result.

        Returns:
            Updated SagaExecution or None if not found.
        """
        if isinstance(execution_id, str):
            execution_id = UUID(execution_id)

        execution = db.get(SagaExecution, execution_id)
        if execution is None:
            logger.warning(
                "Saga execution not found for completion: %s",
                execution_id,
                extra={"event": "saga_execution_not_found"},
            )
            return None

        # Update status based on result
        execution.status = SagaExecutionStatus(result.status.value)
        execution.completed_at = result.completed_at or datetime.now(UTC)
        execution.duration_ms = result.duration_ms

        # Update step tracking
        execution.steps_executed = [s.step_name for s in result.steps_executed]
        execution.steps_compensated = [c.step_name for c in result.steps_compensated]
        execution.compensation_failures = [
            {"step_name": name, "error": error}
            for name, error in result.compensation_failures
        ]

        # Update failure details
        if not result.success:
            execution.failed_step = result.failed_step
            execution.error_message = result.message

        # Store output data
        execution.output_data = result.to_dict()

        db.flush()

        logger.info(
            "Saga execution completed: %s status=%s",
            execution_id,
            execution.status.value,
            extra={
                "event": "saga_execution_completed",
                "saga_name": execution.saga_name,
                "saga_execution_id": str(execution_id),
                "status": execution.status.value,
                "duration_ms": execution.duration_ms,
            },
        )

        return execution

    def get(self, db: Session, execution_id: str | UUID) -> SagaExecution | None:
        """Get a saga execution by ID.

        Args:
            db: Database session.
            execution_id: Saga execution ID.

        Returns:
            SagaExecution or None if not found.
        """
        if isinstance(execution_id, str):
            execution_id = UUID(execution_id)

        return db.get(SagaExecution, execution_id)

    def get_by_correlation_key(
        self,
        db: Session,
        correlation_key: str,
    ) -> SagaExecution | None:
        """Get a saga execution by correlation key.

        Args:
            db: Database session.
            correlation_key: Correlation key.

        Returns:
            SagaExecution or None if not found.
        """
        stmt = select(SagaExecution).where(
            SagaExecution.correlation_key == correlation_key
        ).order_by(SagaExecution.started_at.desc())

        return db.scalars(stmt).first()

    def list_for_ont(
        self,
        db: Session,
        ont_id: str | UUID,
        *,
        limit: int = 20,
    ) -> list[SagaExecution]:
        """List saga executions for an ONT.

        Args:
            db: Database session.
            ont_id: ONT unit ID.
            limit: Maximum number of results.

        Returns:
            List of SagaExecution records.
        """
        if isinstance(ont_id, str):
            ont_id = UUID(ont_id)

        stmt = (
            select(SagaExecution)
            .where(SagaExecution.ont_unit_id == ont_id)
            .order_by(SagaExecution.started_at.desc())
            .limit(limit)
        )

        return list(db.scalars(stmt).all())

    def list_recent(
        self,
        db: Session,
        *,
        saga_name: str | None = None,
        status: SagaExecutionStatus | None = None,
        limit: int = 50,
    ) -> list[SagaExecution]:
        """List recent saga executions with optional filters.

        Args:
            db: Database session.
            saga_name: Filter by saga name.
            status: Filter by status.
            limit: Maximum number of results.

        Returns:
            List of SagaExecution records.
        """
        stmt = select(SagaExecution).order_by(SagaExecution.started_at.desc())

        if saga_name is not None:
            stmt = stmt.where(SagaExecution.saga_name == saga_name)

        if status is not None:
            stmt = stmt.where(SagaExecution.status == status)

        stmt = stmt.limit(limit)

        return list(db.scalars(stmt).all())

    def list_failed_with_compensation_issues(
        self,
        db: Session,
        *,
        limit: int = 100,
    ) -> list[SagaExecution]:
        """List executions where compensation failed.

        These require manual cleanup and operator attention.

        Args:
            db: Database session.
            limit: Maximum number of results.

        Returns:
            List of SagaExecution records with compensation failures.
        """
        stmt = (
            select(SagaExecution)
            .where(SagaExecution.status == SagaExecutionStatus.compensation_failed)
            .order_by(SagaExecution.started_at.desc())
            .limit(limit)
        )

        return list(db.scalars(stmt).all())

    def count_by_status(
        self,
        db: Session,
        saga_name: str | None = None,
    ) -> dict[str, int]:
        """Count executions by status.

        Args:
            db: Database session.
            saga_name: Optional filter by saga name.

        Returns:
            Dictionary mapping status name to count.
        """
        from sqlalchemy import func

        stmt = select(
            SagaExecution.status,
            func.count(SagaExecution.id),
        ).group_by(SagaExecution.status)

        if saga_name is not None:
            stmt = stmt.where(SagaExecution.saga_name == saga_name)

        results = db.execute(stmt).all()

        return {status.value: count for status, count in results}


# Singleton instance
saga_executions = SagaExecutionRepository()
