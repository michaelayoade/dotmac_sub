"""Local mirror of CRM project/installation data (Installation tracker).

All DB + CRM access for the customer-facing installation tracker lives here so
the API/web wrappers stay thin. The CRM owns projects (and derives the stage
timeline + progress %); this keeps a read-optimised local copy hydrated by:

  * CRM ``project.*`` webhooks (near-real-time status), and
  * a periodic reconcile pull + lazy on-view refresh (full stages/progress).

Reads come from the mirror, so "where's my install?" renders instantly and keeps
working during a CRM outage. Read-only for the customer (no write-through).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project_mirror import ProjectMirror, ProjectSyncState
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError, get_crm_client
from app.services.crm_portal import resolve_crm_subscriber_id

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_TTL_SECONDS = 300  # installs change fast; refresh on view often


# ── parsing helpers ──────────────────────────────────────────────────────────


def _to_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


# ── mirror upserts ───────────────────────────────────────────────────────────


def _upsert_row(
    db: Session,
    *,
    subscriber_id,
    crm_project_id: str,
    name: str | None = None,
    status: str | None = None,
    project_type: str | None = None,
    progress_pct: int | None = None,
    current_stage: str | None = None,
    stages: list | None = None,
    customer_address: str | None = None,
    region: str | None = None,
    start_at: datetime | None = None,
    due_at: datetime | None = None,
    completed_at: datetime | None = None,
    project_created_at: datetime | None = None,
) -> ProjectMirror:
    row = db.scalar(
        select(ProjectMirror).where(ProjectMirror.crm_project_id == crm_project_id)
    )
    if row is None:
        row = ProjectMirror(crm_project_id=crm_project_id, subscriber_id=subscriber_id)
        db.add(row)
    row.subscriber_id = subscriber_id
    if name is not None:
        row.name = name
    if status:
        row.status = status
    if project_type is not None:
        row.project_type = project_type
    if progress_pct is not None:
        row.progress_pct = max(0, min(100, progress_pct))
    if current_stage is not None:
        row.current_stage = current_stage
    if stages is not None:
        row.stages = stages
    if customer_address is not None:
        row.customer_address = customer_address
    if region is not None:
        row.region = region
    if start_at is not None:
        row.start_at = start_at
    if due_at is not None:
        row.due_at = due_at
    if completed_at is not None:
        row.completed_at = completed_at
    if project_created_at is not None:
        row.project_created_at = project_created_at
    return row


def _local_subscriber(db: Session, body: dict) -> Subscriber | None:
    """Resolve a project event to the local subscriber. Prefers the sub's own id
    (CRM sends it as ``subscriber_id``), falling back to the CRM subscriber id."""
    local_id = str(body.get("subscriber_id") or "").strip()
    if local_id:
        try:
            sub = db.get(Subscriber, coerce_uuid(local_id))
        except (ValueError, TypeError):
            sub = None
        if sub is not None:
            return sub
    crm_subscriber_id = str(body.get("crm_subscriber_id") or "").strip()
    if crm_subscriber_id:
        try:
            crm_uuid = coerce_uuid(crm_subscriber_id)
        except (ValueError, TypeError):
            return None
        return db.scalar(
            select(Subscriber).where(Subscriber.crm_subscriber_id == crm_uuid)
        )
    return None


# ── reconcile (pull) ─────────────────────────────────────────────────────────


def reconcile_subscriber(db: Session, subscriber_id: str) -> bool:
    """Pull the subscriber's projects (with derived stages/progress) from the CRM
    into the mirror. Returns True on success, False if not CRM-linked. Raises
    CRMClientError if the CRM is unreachable."""
    crm_subscriber_id = resolve_crm_subscriber_id(db, str(subscriber_id))
    if not crm_subscriber_id:
        return False

    data = get_crm_client().get_portal_projects(crm_subscriber_id)
    sub_uuid = coerce_uuid(str(subscriber_id))

    for item in data.get("projects") or []:
        crm_project_id = str(item.get("id") or "").strip()
        if not crm_project_id:
            continue
        _upsert_row(
            db,
            subscriber_id=sub_uuid,
            crm_project_id=crm_project_id,
            name=item.get("name"),
            status=item.get("status"),
            project_type=item.get("project_type"),
            progress_pct=_to_int(item.get("progress_pct")),
            current_stage=item.get("current_stage"),
            stages=item.get("stages") if isinstance(item.get("stages"), list) else None,
            customer_address=item.get("customer_address"),
            region=item.get("region"),
            start_at=_to_dt(item.get("start_at")),
            due_at=_to_dt(item.get("due_at")),
            completed_at=_to_dt(item.get("completed_at")),
            project_created_at=_to_dt(item.get("created_at")),
        )

    sync = db.get(ProjectSyncState, sub_uuid)
    if sync is None:
        sync = ProjectSyncState(subscriber_id=sub_uuid)
        db.add(sync)
    sync.synced_at = datetime.now(UTC)
    db.commit()
    return True


def reconcile_all(db: Session, *, stale_after_seconds: int = 3600) -> int:
    """Reconcile subscribers whose mirror is stale (periodic task)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=max(60, stale_after_seconds))
    stale = db.scalars(
        select(ProjectSyncState.subscriber_id).where(
            ProjectSyncState.synced_at < cutoff
        )
    ).all()
    done = 0
    for subscriber_id in stale:
        try:
            if reconcile_subscriber(db, str(subscriber_id)):
                done += 1
        except CRMClientError as exc:
            db.rollback()
            logger.warning(
                "project_reconcile_failed subscriber=%s: %s", subscriber_id, exc
            )
    return done


# ── reads ────────────────────────────────────────────────────────────────────


def _enqueue_lazy_refresh(subscriber_id: str) -> None:
    """Enqueue a background mirror refresh (best-effort — the periodic reconcile
    is the backstop, so an enqueue failure must not break the read)."""
    from app.services.queue_adapter import enqueue_task

    try:
        enqueue_task(
            "app.tasks.projects.refresh_project_mirror_for_subscriber",
            args=[subscriber_id],
            source="project_lazy_refresh",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "project_lazy_refresh_enqueue_failed subscriber=%s: %s",
            subscriber_id,
            exc,
        )


def read_for_subscriber(
    db: Session,
    subscriber_id: str,
    *,
    refresh_ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS,
) -> dict:
    """Build the installation-tracker payload from the local mirror, lazily
    refreshing from the CRM when the cache is missing or stale (best-effort)."""
    sub_uuid = coerce_uuid(str(subscriber_id))
    sync = db.get(ProjectSyncState, sub_uuid)
    cutoff = datetime.now(UTC) - timedelta(seconds=max(0, refresh_ttl_seconds))
    synced = _as_utc(sync.synced_at) if sync else None
    if sync is None or synced is None:
        # Cold cache — fetch synchronously so the first load is populated.
        try:
            reconcile_subscriber(db, str(subscriber_id))
        except CRMClientError as exc:
            db.rollback()
            logger.warning(
                "project_lazy_refresh_failed subscriber=%s: %s", sub_uuid, exc
            )
    elif synced < cutoff:
        # Warm but stale — serve the stale copy now and refresh in the background.
        # Optimistically stamp synced_at so concurrent reads within the TTL don't
        # each enqueue (debounce); the refresh task re-stamps after pulling.
        sync.synced_at = datetime.now(UTC)
        db.commit()
        _enqueue_lazy_refresh(str(subscriber_id))

    rows = db.scalars(
        select(ProjectMirror)
        .where(ProjectMirror.subscriber_id == sub_uuid)
        .order_by(ProjectMirror.created_at.desc())
    ).all()

    projects = [
        {
            "id": r.crm_project_id,
            "name": r.name,
            "status": r.status,
            "project_type": r.project_type,
            "progress_pct": r.progress_pct,
            "current_stage": r.current_stage,
            "stages": r.stages or [],
            "customer_address": r.customer_address,
            "region": r.region,
            "start_at": r.start_at.isoformat() if r.start_at else None,
            "due_at": r.due_at.isoformat() if r.due_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "created_at": r.project_created_at.isoformat()
            if r.project_created_at
            else None,
        }
        for r in rows
    ]
    active = sum(1 for r in rows if r.status not in ("completed", "canceled"))
    return {"projects": projects, "total": len(projects), "active": active}


# ── webhook application ───────────────────────────────────────────────────────

_PROJECT_STATUS_EVENTS = {
    "project.created",
    "project.updated",
    "project.completed",
    "project.canceled",
}
_TASK_EVENTS = {"project_task.completed", "project_task.updated"}


def apply_webhook(db: Session, event_type: str, body: dict) -> dict:
    """Apply a CRM project lifecycle event to the mirror.

    project.* events carry the subscriber + status → upsert basic fields (full
    stages/progress are filled by reconcile / lazy refresh). project_task.*
    events carry only project_id → force a refresh of that project's subscriber
    so the next view recomputes progress. Acks unmapped/incomplete events.
    """
    crm_project_id = str(body.get("project_id") or body.get("id") or "").strip()
    if not crm_project_id:
        return {"status": "ignored", "reason": "incomplete_payload"}

    if event_type in _TASK_EVENTS:
        # No subscriber in task events; find the mirrored project and mark its
        # subscriber stale so the next read pulls fresh progress.
        row = db.scalar(
            select(ProjectMirror).where(ProjectMirror.crm_project_id == crm_project_id)
        )
        if row is None:
            return {"status": "ignored", "reason": "project_not_mirrored"}
        sync = db.get(ProjectSyncState, row.subscriber_id)
        if sync is not None:
            sync.synced_at = datetime(1970, 1, 1, tzinfo=UTC)
            db.commit()
        return {"status": "ok", "event": event_type}

    if event_type not in _PROJECT_STATUS_EVENTS:
        return {"status": "ignored", "event": event_type}

    subscriber = _local_subscriber(db, body)
    if subscriber is None:
        logger.warning(
            "crm_project_event_unmapped event=%s project_id=%s",
            event_type,
            crm_project_id,
        )
        return {"status": "ignored", "reason": "unmapped_subscriber"}

    completed_at = None
    if event_type == "project.completed":
        completed_at = _to_dt(body.get("completed_at")) or datetime.now(UTC)

    _upsert_row(
        db,
        subscriber_id=subscriber.id,
        crm_project_id=crm_project_id,
        name=body.get("name"),
        status=body.get("status") or body.get("to_status"),
        project_type=body.get("project_type"),
        region=body.get("region"),
        completed_at=completed_at,
    )
    # New/changed project → mark stale so the next read pulls full stages.
    sync = db.get(ProjectSyncState, subscriber.id)
    if sync is not None:
        sync.synced_at = datetime(1970, 1, 1, tzinfo=UTC)
    db.commit()

    # Proactively tell the customer when their installation completes (mirrors
    # the work-order push). Best-effort: a push failure never breaks the mirror.
    if event_type == "project.completed":
        try:
            from app.services import push as push_service

            push_service.send_push(
                db,
                str(subscriber.id),
                title="Installation complete",
                body="Your installation project is now complete.",
                data={"type": "project", "project_id": crm_project_id},
            )
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            logger.warning("project_push_failed project_id=%s: %s", crm_project_id, exc)

    return {"status": "ok", "event": event_type}
