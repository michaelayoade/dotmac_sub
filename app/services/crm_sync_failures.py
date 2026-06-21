"""Dead-letter management for terminal CRM push failures.

Visibility (count + list) and re-drive: re-enqueue the original push through
the retrying task and mark resolved when it lands. A daily bounded sweep
re-drives unresolved rows so a multi-hour CRM outage self-recovers without
manual action.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.crm_sync_failure import CrmSyncFailure, CrmSyncFailureStatus

logger = logging.getLogger(__name__)


def unresolved_count(db: Session) -> int:
    return int(
        db.query(func.count(CrmSyncFailure.id))
        .filter(CrmSyncFailure.status == CrmSyncFailureStatus.unresolved)
        .scalar()
        or 0
    )


def list_failures(
    db: Session, *, unresolved_only: bool = True, limit: int = 100
) -> list[CrmSyncFailure]:
    query = db.query(CrmSyncFailure)
    if unresolved_only:
        query = query.filter(CrmSyncFailure.status == CrmSyncFailureStatus.unresolved)
    return query.order_by(CrmSyncFailure.created_at.desc()).limit(limit).all()


def _redrive_row(db: Session, failure: CrmSyncFailure) -> bool:
    """Re-enqueue one failed push. Returns True if dispatched."""
    if failure.payload is None:
        return False
    from app.tasks.crm_sync import push_subscriber_change as push_task

    push_task.delay(failure.external_id, failure.payload, failure.external_system)
    failure.status = CrmSyncFailureStatus.resolved
    failure.resolved_at = datetime.now(UTC)
    db.commit()
    return True


def redrive(db: Session, failure_id: str) -> bool:
    failure = db.get(CrmSyncFailure, failure_id)
    if not failure or failure.status != CrmSyncFailureStatus.unresolved:
        return False
    return _redrive_row(db, failure)


def redrive_all(db: Session, *, limit: int = 500) -> dict:
    """Daily sweep: re-enqueue every unresolved failure (bounded).

    Marks each resolved on dispatch; if the re-driven push fails again it
    lands a fresh dead-letter row, so a persistently-down CRM stays visible
    rather than silently looping.
    """
    rows = db.scalars(
        select(CrmSyncFailure)
        .where(CrmSyncFailure.status == CrmSyncFailureStatus.unresolved)
        .order_by(CrmSyncFailure.created_at)
        .limit(limit)
    ).all()
    redriven = sum(1 for row in rows if _redrive_row(db, row))
    if redriven:
        logger.info("CRM dead-letter sweep re-drove %d failures", redriven)
    return {"redriven": redriven}
