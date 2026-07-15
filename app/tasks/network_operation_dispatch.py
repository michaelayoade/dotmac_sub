"""Celery adapters for the durable network-operation dispatch outbox."""

from __future__ import annotations

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.network_operation_dispatch import (
    publish_ready_dispatches,
    reconcile_dispatches,
)
from app.services.observability import record_metric, record_task_run


@celery_app.task(
    name="app.tasks.network_operation_dispatch.publish_network_operation_dispatches"
)
def publish_network_operation_dispatches() -> dict[str, int]:
    """Repair stale state, then publish due command envelopes."""
    task_name = (
        "app.tasks.network_operation_dispatch.publish_network_operation_dispatches"
    )
    try:
        with db_session_adapter.session() as db:
            reconciled = reconcile_dispatches(db)
            published = publish_ready_dispatches(db)
            result = published.as_dict()
            result["reconciled"] = reconciled.completed + reconciled.canceled
            result["reconciliation_needed"] += reconciled.reconciliation_needed
            status = (
                "degraded"
                if result["failed"] or result["reconciliation_needed"]
                else "ok"
            )
            record_task_run(task_name, status=status, counters=result)
            record_metric(
                domain="network_operations",
                signal="dispatch_sweep",
                status=status,
            )
            return result
    except Exception:
        record_task_run(task_name, status="error")
        record_metric(
            domain="network_operations",
            signal="dispatch_sweep",
            status="error",
        )
        raise
