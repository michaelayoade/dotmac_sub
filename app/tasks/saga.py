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
    correlation_key: str | None = None,
    bulk_run_id: str | None = None,
    bulk_item_id: str | None = None,
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
        correlation_key: Optional correlation key for events and saga persistence.
        bulk_run_id: Optional bulk provisioning run ID.
        bulk_item_id: Optional bulk provisioning item ID.

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
        if bulk_item_id:
            from app.services.network.bulk_provisioning import mark_bulk_item_running

            mark_bulk_item_running(
                session,
                bulk_item_id,
                saga_execution_id=execution_id,
            )
            session.commit()

        # Look up saga by name
        saga = get_saga_by_name(saga_name)
        if saga is None:
            if bulk_item_id:
                from app.services.network.bulk_provisioning import mark_bulk_item_failed

                mark_bulk_item_failed(
                    session,
                    bulk_item_id,
                    f"Saga not found: {saga_name}",
                )
                session.commit()
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
                "bulk_run_id": bulk_run_id,
                "bulk_item_id": bulk_item_id,
            }

        effective_correlation_key = correlation_key
        if effective_correlation_key is None and bulk_item_id:
            from app.models.network import BulkProvisioningItem

            item = session.get(BulkProvisioningItem, bulk_item_id)
            if item is not None:
                effective_correlation_key = item.correlation_key

        # Build context
        context = SagaContext(
            db=session,
            ont_id=ont_id,
            saga_execution_id=execution_id,
            step_data=step_data or {},
            dry_run=dry_run,
            initiated_by=initiated_by,
            correlation_key=effective_correlation_key,
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

        if effective_correlation_key:
            from app.services.network.provisioning_events import (
                provisioning_correlation,
            )

        # Execute saga
        executor = SagaExecutor(saga, context)
        if effective_correlation_key:
            with provisioning_correlation(effective_correlation_key):
                result = executor.execute()
        else:
            result = executor.execute()

        # Persist result
        if persist_execution:
            saga_executions.mark_completed(session, execution_id, result)
            session.commit()

        if bulk_item_id:
            from app.services.network.bulk_provisioning import mark_bulk_item_completed

            mark_bulk_item_completed(session, bulk_item_id, result.to_dict())
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

        payload = result.to_dict()
        payload.update(
            {
                "bulk_run_id": bulk_run_id,
                "bulk_item_id": bulk_item_id,
                "correlation_key": effective_correlation_key,
            }
        )
        return payload

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

        if bulk_item_id:
            try:
                from app.services.network.bulk_provisioning import mark_bulk_item_failed

                mark_bulk_item_failed(session, bulk_item_id, str(exc))
                session.commit()
            except Exception:
                logger.warning(
                    "Failed to mark bulk provisioning item %s as failed",
                    bulk_item_id,
                    exc_info=True,
                )

        return {
            "success": False,
            "message": f"Saga task error: {exc}",
            "saga_name": saga_name,
            "saga_execution_id": execution_id,
            "bulk_run_id": bulk_run_id,
            "bulk_item_id": bulk_item_id,
            "correlation_key": correlation_key,
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
    bulk_run_id: str | None = None,
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

    bulk_items_by_ont_id: dict[str, Any] = {}
    if bulk_run_id:
        session = SessionLocal()
        try:
            from app.services.network.bulk_provisioning import list_pending_bulk_items

            pending_items = list_pending_bulk_items(session, bulk_run_id)
            bulk_items_by_ont_id = {
                str(item.ont_unit_id): item for item in pending_items if item.ont_unit_id
            }
        finally:
            session.close()

    unique_ont_ids = list(dict.fromkeys(str(ont_id) for ont_id in ont_ids if ont_id))
    if bulk_items_by_ont_id:
        unique_ont_ids = [
            ont_id for ont_id in unique_ont_ids if ont_id in bulk_items_by_ont_id
        ]
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
        bulk_item = bulk_items_by_ont_id.get(ont_id)
        item_correlation_key = (
            getattr(bulk_item, "correlation_key", None)
            if bulk_item is not None
            else f"bulk_saga:{saga_name}:{ont_id}"
        )
        dispatch = enqueue_celery_task(
            execute_saga_task,
            kwargs={
                "saga_name": saga_name,
                "ont_id": ont_id,
                "step_data": dict(step_data or {}),
                "dry_run": dry_run,
                "initiated_by": initiated_by,
                "correlation_key": item_correlation_key,
                "bulk_run_id": bulk_run_id,
                "bulk_item_id": str(bulk_item.id) if bulk_item is not None else None,
            },
            correlation_id=item_correlation_key,
            source="bulk_saga_orchestrator",
            countdown=countdown,
        )
        tasks.append(
            {
                "ont_id": ont_id,
                "task_id": str(dispatch.id),
                "chunk": chunk_index + 1,
                "countdown": countdown,
                "bulk_item_id": str(bulk_item.id) if bulk_item is not None else "",
                "correlation_key": item_correlation_key,
            }
        )

    stats = {
        "queued": len(tasks),
        "errors": 0,
        "skipped": len(ont_ids) - len(unique_ont_ids),
        "saga_name": saga_name,
        "max_parallel": max_parallel,
        "chunks": total_chunks,
        "bulk_run_id": bulk_run_id,
        "tasks": tasks,
    }
    logger.info("Bulk saga queued: %s", stats)
    return stats
