"""Celery tasks for network operation maintenance."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult

from app.celery_app import celery_app
from app.models.network_operation import NetworkOperation, NetworkOperationStatus
from app.services.db_session_adapter import db_session_adapter
from app.services.network_operations import (
    _mark_operation_stale_failed,
    _operation_is_stale_active,
    network_operations,
)
from app.services.observability import (
    StateObservation,
    publish_state_snapshot,
    record_task_run,
)

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session

_RETENTION_DAYS = 90


@celery_app.task(name="app.tasks.network_operations.cleanup_old_operations")
def cleanup_old_operations() -> dict[str, int]:
    """Purge completed operations older than the retention period.

    Also marks operations stuck in 'running', 'pending', or 'waiting'
    for longer than the stale threshold as 'failed'.

    Returns:
        Statistics dict with purged, stale_marked, errors.
    """
    logger.info("Starting network operations cleanup")
    db = SessionLocal()
    try:
        cutoff = datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)
        now = datetime.now(UTC)

        # Purge old completed operations, excluding parents with active children
        # (CASCADE on parent_id would delete in-progress children otherwise).
        # Use aliased child table to correlate the subquery correctly.
        from sqlalchemy import exists
        from sqlalchemy.orm import aliased

        ChildOp = aliased(NetworkOperation)  # noqa: N806
        RedriveOp = aliased(NetworkOperation)  # noqa: N806
        has_active_children = exists(
            select(ChildOp.id).where(
                ChildOp.parent_id == NetworkOperation.id,
                ChildOp.status.in_(
                    [
                        NetworkOperationStatus.running,
                        NetworkOperationStatus.pending,
                        NetworkOperationStatus.waiting,
                    ]
                ),
            )
        )
        has_redrive_attempts = exists(
            select(RedriveOp.id).where(RedriveOp.redrive_of_id == NetworkOperation.id)
        )
        purge_stmt = delete(NetworkOperation).where(
            NetworkOperation.status.in_(
                [
                    NetworkOperationStatus.succeeded,
                    NetworkOperationStatus.failed,
                    NetworkOperationStatus.canceled,
                ]
            ),
            NetworkOperation.completed_at < cutoff,
            ~has_active_children,
            ~has_redrive_attempts,
        )
        purge_result = cast(CursorResult[Any], db.execute(purge_stmt))
        purged = purge_result.rowcount

        # Mark stale running/pending/waiting operations as failed.
        # Direct status mutation is safe here: the WHERE clause guarantees
        # only non-terminal statuses are selected, so _check_not_terminal
        # is redundant. We skip the service layer for batch efficiency.
        stale_stmt = select(NetworkOperation).where(
            NetworkOperation.status.in_(
                [
                    NetworkOperationStatus.running,
                    NetworkOperationStatus.pending,
                    NetworkOperationStatus.waiting,
                ]
            )
        )
        stale_ops = list(db.scalars(stale_stmt).all())
        stale_marked = 0
        for op in stale_ops:
            if not _operation_is_stale_active(op, now=now):
                continue
            _mark_operation_stale_failed(op, now=now)
            stale_marked += 1
            logger.warning(
                "Marked stale operation %s (%s) as failed",
                op.id,
                op.operation_type.value if op.operation_type else "unknown",
            )

        result = {"purged": purged, "stale_marked": stale_marked, "errors": 0}
        logger.info("Network operations cleanup complete: %s", result)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.network_operations.publish_operation_metrics")
def publish_operation_metrics() -> dict[str, float]:
    """Publish bounded operation and recovery health from the durable ledger."""
    db = SessionLocal()
    try:
        from app.services.network_operation_dispatch import health_snapshot

        snapshot = {
            **network_operations.health_snapshot(db),
            **health_snapshot(db),
        }
        observations: list[StateObservation] = []
        for key, value in sorted(snapshot.items()):
            if key.startswith("operations_"):
                signal, scope = "operations", key.removeprefix("operations_")
            elif key.startswith("redrives_"):
                signal, scope = "redrives", key.removeprefix("redrives_")
            elif key.startswith("dispatches_"):
                signal, scope = "dispatches", key.removeprefix("dispatches_")
            else:
                signal, scope = key, "all"
            observations.append(
                StateObservation(signal=signal, scope=scope, value=float(value))
            )
        stored = publish_state_snapshot("network_operations", observations)
        result = {**snapshot, "snapshot_stored": float(stored)}
        record_task_run(
            "app.tasks.network_operations.publish_operation_metrics",
            status="ok" if stored else "degraded",
            counters=result,
        )
        return result
    except Exception:
        record_task_run(
            "app.tasks.network_operations.publish_operation_metrics",
            status="error",
        )
        raise
    finally:
        db.close()
