"""Bulk ONT provisioning audit and Celery dispatch."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.network import (
    BulkProvisioningItem,
    BulkProvisioningItemStatus,
    BulkProvisioningRun,
    BulkProvisioningRunStatus,
    OntProvisioningEvent,
    OntUnit,
)


@dataclass(frozen=True)
class BulkProvisioningDispatchResult:
    """Result returned after a bulk provisioning run is queued."""

    run_id: uuid.UUID
    correlation_key: str
    status: BulkProvisioningRunStatus
    total: int
    queued: int
    skipped: int
    orchestrator_task_id: str | None


def _as_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _dedupe_ont_ids(ont_ids: list[str | uuid.UUID]) -> tuple[list[uuid.UUID], int]:
    unique: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    skipped = 0
    for raw_id in ont_ids:
        try:
            ont_id = _as_uuid(raw_id)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if ont_id in seen:
            skipped += 1
            continue
        seen.add(ont_id)
        unique.append(ont_id)
    return unique, skipped


def _status_from_result(result: dict[str, Any]) -> BulkProvisioningItemStatus:
    if result.get("success"):
        return BulkProvisioningItemStatus.succeeded
    return BulkProvisioningItemStatus.failed


def _run_status(
    *,
    succeeded: int,
    failed: int,
    skipped: int,
    total: int,
) -> BulkProvisioningRunStatus:
    if total == 0:
        return BulkProvisioningRunStatus.failed
    if failed == 0 and skipped == 0 and succeeded == total:
        return BulkProvisioningRunStatus.succeeded
    if succeeded > 0:
        return BulkProvisioningRunStatus.partial
    return BulkProvisioningRunStatus.failed


def _create_bulk_run(
    db: Session,
    *,
    ont_ids: list[uuid.UUID],
    provisioning_mode: str,
    max_workers: int,
    initiated_by: str | None,
    correlation_key: str,
    metadata: dict[str, Any] | None,
    input_skipped_count: int,
) -> BulkProvisioningRun:
    run_metadata = dict(metadata or {})
    run_metadata.update(
        {
            "provisioning_mode": provisioning_mode,
            "input_skipped_count": input_skipped_count,
        }
    )
    run = BulkProvisioningRun(
        profile_id=None,
        status=BulkProvisioningRunStatus.pending,
        correlation_key=correlation_key,
        initiated_by=initiated_by,
        max_workers=max_workers,
        total_count=len(ont_ids) + input_skipped_count,
        skipped_count=input_skipped_count,
        run_metadata=run_metadata,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.flush()

    existing_ont_ids = set(
        db.scalars(select(OntUnit.id).where(OntUnit.id.in_(ont_ids))).all()
    )
    for ont_id in ont_ids:
        exists = ont_id in existing_ont_ids
        db.add(
            BulkProvisioningItem(
                run_id=run.id,
                requested_ont_id=str(ont_id),
                ont_unit_id=ont_id if exists else None,
                status=(
                    BulkProvisioningItemStatus.pending
                    if exists
                    else BulkProvisioningItemStatus.skipped
                ),
                correlation_key=f"{correlation_key}:ont:{ont_id}",
                message=None if exists else "ONT not found",
                error_message=None if exists else "ONT not found",
                completed_at=None if exists else datetime.now(UTC),
            )
        )
        if not exists:
            run.skipped_count += 1
    db.flush()
    return run


def bulk_provision_onts(
    db: Session,
    ont_ids: list[str | uuid.UUID],
    *,
    provisioning_mode: str = "direct",
    tr069_olt_profile_id: int | None = None,
    max_workers: int = 10,
    chunk_delay_seconds: int = 15,
    initiated_by: str | None = None,
    correlation_key: str | None = None,
    dry_run: bool = False,
    allow_low_optical_margin: bool = False,
    step_data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> BulkProvisioningDispatchResult:
    """Create a durable bulk run and queue direct provisioning tasks."""
    del provisioning_mode
    from app.services.queue_adapter import enqueue_task

    workers = max(1, min(int(max_workers or 10), 50))
    delay = max(0, int(chunk_delay_seconds or 0))
    unique_ont_ids, input_skipped_count = _dedupe_ont_ids(ont_ids)
    run_correlation = correlation_key or f"bulk_provision:{uuid.uuid4()}"
    run = _create_bulk_run(
        db,
        ont_ids=unique_ont_ids,
        provisioning_mode="direct",
        max_workers=workers,
        initiated_by=initiated_by,
        correlation_key=run_correlation,
        metadata=metadata,
        input_skipped_count=input_skipped_count,
    )

    queued_count = db.scalar(
        select(func.count())
        .select_from(BulkProvisioningItem)
        .where(BulkProvisioningItem.run_id == run.id)
        .where(BulkProvisioningItem.status == BulkProvisioningItemStatus.pending)
    )
    if queued_count is None:
        queued_count = 0

    orchestrator_task_id: str | None = None
    if queued_count:
        dispatch = enqueue_task(
            "app.tasks.ont_provisioning.queue_bulk_provisioning",
            kwargs={
                "ont_ids": [str(ont_id) for ont_id in unique_ont_ids],
                "tr069_olt_profile_id": tr069_olt_profile_id,
                "dry_run": dry_run,
                "initiated_by": initiated_by,
                "max_parallel": workers,
                "chunk_delay_seconds": delay,
                "bulk_run_id": str(run.id),
                "allow_low_optical_margin": allow_low_optical_margin,
                "wait_for_acs": bool((step_data or {}).get("wait_for_acs", True)),
                "apply_acs_config": bool((step_data or {}).get("apply_acs_config", True)),
            },
            correlation_id=run_correlation,
            source="bulk_provisioning_service",
        )
        orchestrator_task_id = str(dispatch.task_id or "")
        run.status = BulkProvisioningRunStatus.running
        run.run_metadata = {
            **(run.run_metadata or {}),
            "orchestrator_task_id": orchestrator_task_id,
        }
    else:
        finalize_bulk_provisioning_run(db, run.id)

    db.commit()
    return BulkProvisioningDispatchResult(
        run_id=run.id,
        correlation_key=run.correlation_key,
        status=run.status,
        total=run.total_count,
        queued=int(queued_count),
        skipped=run.skipped_count,
        orchestrator_task_id=orchestrator_task_id,
    )


def list_pending_bulk_items(
    db: Session,
    run_id: str | uuid.UUID,
) -> list[BulkProvisioningItem]:
    """Return pending items for a bulk run in creation order."""
    run_uuid = _as_uuid(run_id)
    stmt = (
        select(BulkProvisioningItem)
        .where(BulkProvisioningItem.run_id == run_uuid)
        .where(BulkProvisioningItem.status == BulkProvisioningItemStatus.pending)
        .where(BulkProvisioningItem.ont_unit_id.is_not(None))
        .order_by(BulkProvisioningItem.created_at)
    )
    return list(db.scalars(stmt).all())


def mark_bulk_item_running(
    db: Session,
    item_id: str | uuid.UUID,
    *,
    provisioning_execution_id: str | None = None,
) -> BulkProvisioningItem | None:
    """Mark a bulk item as running."""
    item = db.get(BulkProvisioningItem, _as_uuid(item_id))
    if item is None:
        return None
    item.status = BulkProvisioningItemStatus.running
    item.started_at = item.started_at or datetime.now(UTC)
    item.result_data = {
        **(item.result_data or {}),
        **(
            {"provisioning_execution_id": provisioning_execution_id}
            if provisioning_execution_id
            else {}
        ),
    }
    db.flush()
    return item


def mark_bulk_item_completed(
    db: Session,
    item_id: str | uuid.UUID,
    result: dict[str, Any],
) -> BulkProvisioningItem | None:
    """Persist a child provisioning result onto its bulk item and refresh status."""
    item = db.get(BulkProvisioningItem, _as_uuid(item_id))
    if item is None:
        return None
    item.status = _status_from_result(result)
    item.message = str(result.get("message") or "")
    item.error_message = None if result.get("success") else item.message
    item.result_data = {
        **(item.result_data or {}),
        "provisioning_result": result,
    }
    item.completed_at = datetime.now(UTC)
    db.flush()
    finalize_bulk_provisioning_run(db, item.run_id)
    return item


def mark_bulk_item_failed(
    db: Session,
    item_id: str | uuid.UUID,
    error: str,
) -> BulkProvisioningItem | None:
    """Persist a task-level failure onto its bulk item and refresh run status."""
    item = db.get(BulkProvisioningItem, _as_uuid(item_id))
    if item is None:
        return None
    item.status = BulkProvisioningItemStatus.failed
    item.message = error
    item.error_message = error
    item.completed_at = datetime.now(UTC)
    db.flush()
    finalize_bulk_provisioning_run(db, item.run_id)
    return item


def finalize_bulk_provisioning_run(
    db: Session,
    run_id: str | uuid.UUID,
) -> BulkProvisioningRun:
    """Refresh aggregate counters and close the run when no items are active."""
    run = db.get(BulkProvisioningRun, _as_uuid(run_id))
    if run is None:
        raise ValueError(f"Bulk provisioning run not found: {run_id}")

    items = list(
        db.scalars(
            select(BulkProvisioningItem).where(BulkProvisioningItem.run_id == run.id)
        ).all()
    )
    run.succeeded_count = sum(
        1 for item in items if item.status == BulkProvisioningItemStatus.succeeded
    )
    run.failed_count = sum(
        1 for item in items if item.status == BulkProvisioningItemStatus.failed
    )
    input_skipped_count = int((run.run_metadata or {}).get("input_skipped_count") or 0)
    run.skipped_count = input_skipped_count + sum(
        1 for item in items if item.status == BulkProvisioningItemStatus.skipped
    )
    active_count = sum(
        1
        for item in items
        if item.status
        in {BulkProvisioningItemStatus.pending, BulkProvisioningItemStatus.running}
    )
    if active_count:
        run.status = BulkProvisioningRunStatus.running
    else:
        run.status = _run_status(
            succeeded=run.succeeded_count,
            failed=run.failed_count,
            skipped=run.skipped_count,
            total=run.total_count,
        )
        run.completed_at = datetime.now(UTC)
    db.flush()
    return run


def get_bulk_provisioning_run(
    db: Session,
    run_id: str | uuid.UUID,
) -> BulkProvisioningRun | None:
    """Load a bulk run with items."""
    stmt = (
        select(BulkProvisioningRun)
        .options(selectinload(BulkProvisioningRun.items))
        .where(BulkProvisioningRun.id == _as_uuid(run_id))
    )
    return db.scalars(stmt).first()


def list_bulk_provisioning_events(
    db: Session,
    run_id: str | uuid.UUID,
) -> list[OntProvisioningEvent]:
    """Return all ONT provisioning events associated with a bulk run."""
    run = db.get(BulkProvisioningRun, _as_uuid(run_id))
    if run is None:
        return []
    stmt = (
        select(OntProvisioningEvent)
        .where(OntProvisioningEvent.correlation_key.like(f"{run.correlation_key}:%"))
        .order_by(OntProvisioningEvent.created_at)
    )
    return list(db.scalars(stmt).all())
