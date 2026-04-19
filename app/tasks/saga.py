"""Celery tasks for saga execution.

Provides background task support for saga-based provisioning workflows.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.celery_app import celery_app
from app.db import SessionLocal

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.saga.execute_saga",
    bind=True,
    max_retries=0,  # Sagas handle their own compensation, no retry
)
def execute_saga_task(
    self,
    saga_name: str,
    ont_id: str,
    *,
    step_data: dict[str, Any] | None = None,
    dry_run: bool = False,
    initiated_by: str | None = None,
    persist_execution: bool = True,
) -> dict:
    """Execute a saga in background with full compensation support.

    This task executes a registered saga and persists the execution history.
    On failure, compensation actions are automatically run in reverse order.

    Args:
        saga_name: Name of the saga from SAGA_REGISTRY.
        ont_id: Target ONT unit ID.
        step_data: Optional data to pass to saga steps.
        dry_run: If True, steps should not make real changes.
        initiated_by: User or system identifier.
        persist_execution: If True, persist execution to database.

    Returns:
        Dictionary with saga execution result.
    """
    from app.services.network.ont_provisioning.saga import (
        SagaContext,
        SagaExecutor,
        generate_saga_execution_id,
        get_saga_by_name,
        saga_executions,
    )

    session = SessionLocal()
    execution_id = generate_saga_execution_id()

    try:
        # Look up saga by name
        saga = get_saga_by_name(saga_name)
        if saga is None:
            logger.error(
                "Saga not found: %s",
                saga_name,
                extra={"event": "saga_task_saga_not_found"},
            )
            return {
                "success": False,
                "message": f"Saga not found: {saga_name}",
                "saga_name": saga_name,
                "saga_execution_id": execution_id,
            }

        # Build context
        context = SagaContext(
            db=session,
            ont_id=ont_id,
            saga_execution_id=execution_id,
            step_data=step_data or {},
            dry_run=dry_run,
            initiated_by=initiated_by,
        )

        # Persist execution record
        if persist_execution:
            saga_executions.create(session, saga, context)
            saga_executions.mark_running(session, execution_id)
            session.commit()

        logger.info(
            "Starting saga task: %s (execution_id=%s, ont_id=%s)",
            saga_name,
            execution_id,
            ont_id,
            extra={
                "event": "saga_task_start",
                "saga_name": saga_name,
                "saga_execution_id": execution_id,
                "ont_id": ont_id,
                "celery_task_id": self.request.id,
            },
        )

        # Execute saga
        executor = SagaExecutor(saga, context)
        result = executor.execute()

        # Persist result
        if persist_execution:
            saga_executions.mark_completed(session, execution_id, result)
            session.commit()

        # Send WebSocket notification
        _notify_saga_complete(result)

        logger.info(
            "Saga task completed: %s success=%s (%dms)",
            saga_name,
            result.success,
            result.duration_ms,
            extra={
                "event": "saga_task_complete",
                "saga_name": saga_name,
                "saga_execution_id": execution_id,
                "success": result.success,
                "duration_ms": result.duration_ms,
            },
        )

        return result.to_dict()

    except Exception as exc:
        session.rollback()
        logger.error(
            "Saga task failed: %s - %s",
            saga_name,
            exc,
            exc_info=True,
            extra={
                "event": "saga_task_error",
                "saga_name": saga_name,
                "saga_execution_id": execution_id,
            },
        )

        # Try to mark execution as failed
        if persist_execution:
            try:
                from app.models.saga_execution import (
                    SagaExecution,
                    SagaExecutionStatus,
                )

                execution = session.get(SagaExecution, execution_id)
                if execution:
                    execution.status = SagaExecutionStatus.failed
                    execution.error_message = str(exc)
                    session.commit()
            except Exception:
                pass

        return {
            "success": False,
            "message": f"Saga task error: {exc}",
            "saga_name": saga_name,
            "saga_execution_id": execution_id,
        }

    finally:
        session.close()


def _notify_saga_complete(result) -> None:
    """Send WebSocket notification for saga completion."""
    try:
        from app.services.notification_adapter import notify

        if result.success:
            notify.send(
                channel="websocket",
                recipient="broadcast",
                message=f"Saga '{result.saga_name}' completed successfully",
                title="Provisioning Complete",
                category="provisioning",
                metadata={
                    "saga_name": result.saga_name,
                    "saga_execution_id": result.saga_execution_id,
                    "duration_ms": result.duration_ms,
                },
            )
        else:
            severity = "critical" if result.compensation_failures else "error"
            notify.alert_operators(
                title="Provisioning Failed",
                message=f"Saga '{result.saga_name}' failed: {result.message}",
                severity=severity,
                metadata={
                    "saga_name": result.saga_name,
                    "saga_execution_id": result.saga_execution_id,
                    "failed_step": result.failed_step,
                    "compensation_failures": result.steps_needing_manual_cleanup,
                },
            )
    except Exception as exc:
        logger.warning(
            "Failed to send saga notification: %s",
            exc,
            extra={"event": "saga_notification_failed"},
        )


@celery_app.task(
    name="app.tasks.saga.queue_saga_execution",
)
def queue_saga_execution(
    saga_name: str,
    ont_id: str,
    *,
    step_data: dict[str, Any] | None = None,
    initiated_by: str | None = None,
) -> dict:
    """Queue a saga for background execution.

    This is a wrapper that queues execute_saga_task and returns immediately.
    Useful for web handlers that need to return quickly.

    Args:
        saga_name: Name of the saga from SAGA_REGISTRY.
        ont_id: Target ONT unit ID.
        step_data: Optional data to pass to saga steps.
        initiated_by: User or system identifier.

    Returns:
        Dictionary with queued task info.
    """
    from app.celery_app import enqueue_celery_task
    from app.services.network.ont_provisioning.saga import generate_saga_execution_id

    execution_id = generate_saga_execution_id()
    correlation_key = f"saga:{saga_name}:{ont_id}:{execution_id}"

    result = enqueue_celery_task(
        execute_saga_task,
        kwargs={
            "saga_name": saga_name,
            "ont_id": ont_id,
            "step_data": step_data,
            "initiated_by": initiated_by,
        },
        correlation_id=correlation_key,
        source="saga_queue",
    )

    logger.info(
        "Queued saga execution: %s (task_id=%s)",
        saga_name,
        result.id,
        extra={
            "event": "saga_queued",
            "saga_name": saga_name,
            "ont_id": ont_id,
            "celery_task_id": str(result.id),
        },
    )

    return {
        "queued": True,
        "saga_name": saga_name,
        "ont_id": ont_id,
        "task_id": str(result.id),
        "correlation_key": correlation_key,
    }


@celery_app.task(name="app.tasks.saga.queue_bulk_saga_executions")
def queue_bulk_saga_executions(
    saga_name: str,
    ont_ids: list[str],
    *,
    step_data: dict[str, Any] | None = None,
    dry_run: bool = False,
    initiated_by: str | None = None,
    max_parallel: int = 10,
    chunk_delay_seconds: int = 15,
) -> dict[str, Any]:
    """Queue saga executions for many ONTs with bounded fan-out.

    This orchestrator deliberately does not execute sagas in-process.  Each ONT
    gets its own Celery task, DB session, saga execution record, and
    compensation lifecycle.  ``max_parallel`` controls how many child tasks are
    released immediately per chunk; later chunks receive a small countdown to
    avoid stampeding OLT/ACS dependencies.
    """
    from app.celery_app import enqueue_celery_task
    from app.services.network.ont_provisioning.saga import get_saga_by_name

    if get_saga_by_name(saga_name) is None:
        return {
            "queued": 0,
            "errors": 1,
            "skipped": len(ont_ids),
            "message": f"Saga not found: {saga_name}",
            "tasks": [],
        }

    unique_ont_ids = list(dict.fromkeys(str(ont_id) for ont_id in ont_ids if ont_id))
    if not unique_ont_ids:
        return {
            "queued": 0,
            "errors": 0,
            "skipped": 0,
            "message": "No ONTs supplied.",
            "tasks": [],
        }

    max_parallel = max(1, min(int(max_parallel or 10), 50))
    chunk_delay_seconds = max(0, int(chunk_delay_seconds or 0))
    total_chunks = math.ceil(len(unique_ont_ids) / max_parallel)
    tasks: list[dict[str, str | int]] = []

    for index, ont_id in enumerate(unique_ont_ids):
        chunk_index = index // max_parallel
        countdown = chunk_index * chunk_delay_seconds
        dispatch = enqueue_celery_task(
            execute_saga_task,
            kwargs={
                "saga_name": saga_name,
                "ont_id": ont_id,
                "step_data": dict(step_data or {}),
                "dry_run": dry_run,
                "initiated_by": initiated_by,
            },
            correlation_id=f"bulk_saga:{saga_name}:{ont_id}",
            source="bulk_saga_orchestrator",
            countdown=countdown,
        )
        tasks.append(
            {
                "ont_id": ont_id,
                "task_id": str(dispatch.id),
                "chunk": chunk_index + 1,
                "countdown": countdown,
            }
        )

    stats = {
        "queued": len(tasks),
        "errors": 0,
        "skipped": len(ont_ids) - len(unique_ont_ids),
        "saga_name": saga_name,
        "max_parallel": max_parallel,
        "chunks": total_chunks,
        "tasks": tasks,
    }
    logger.info("Bulk saga queued: %s", stats)
    return stats
