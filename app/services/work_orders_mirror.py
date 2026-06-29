"""Local mirror of CRM work-order data (Field Service tracker).

All DB + CRM access for the customer-facing field-service tracker lives here so
the API/web wrappers stay thin. The CRM owns work orders; this keeps a
read-optimised local copy hydrated by CRM ``work_order.*`` webhooks + a periodic
reconcile pull + lazy on-view refresh. Read-only for the customer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror, WorkOrderSyncState
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError, get_crm_client
from app.services.crm_portal import resolve_crm_subscriber_id

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_TTL_SECONDS = 180  # "where's my technician" — refresh often


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


def _to_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _upsert_row(
    db: Session,
    *,
    subscriber_id,
    crm_work_order_id: str,
    title: str | None = None,
    status: str | None = None,
    work_type: str | None = None,
    priority: str | None = None,
    technician_name: str | None = None,
    technician_phone: str | None = None,
    address: str | None = None,
    scheduled_start: datetime | None = None,
    scheduled_end: datetime | None = None,
    estimated_arrival_at: datetime | None = None,
    estimated_duration_minutes: int | None = None,
    completed_at: datetime | None = None,
    work_order_created_at: datetime | None = None,
) -> WorkOrderMirror:
    row = db.scalar(
        select(WorkOrderMirror).where(
            WorkOrderMirror.crm_work_order_id == crm_work_order_id
        )
    )
    if row is None:
        row = WorkOrderMirror(
            crm_work_order_id=crm_work_order_id, subscriber_id=subscriber_id
        )
        db.add(row)
    row.subscriber_id = subscriber_id
    if title is not None:
        row.title = title
    if status:
        row.status = status
    if work_type is not None:
        row.work_type = work_type
    if priority is not None:
        row.priority = priority
    if technician_name is not None:
        row.technician_name = technician_name
    if technician_phone is not None:
        row.technician_phone = technician_phone
    if address is not None:
        row.address = address
    if scheduled_start is not None:
        row.scheduled_start = scheduled_start
    if scheduled_end is not None:
        row.scheduled_end = scheduled_end
    if estimated_arrival_at is not None:
        row.estimated_arrival_at = estimated_arrival_at
    if estimated_duration_minutes is not None:
        row.estimated_duration_minutes = estimated_duration_minutes
    if completed_at is not None:
        row.completed_at = completed_at
    if work_order_created_at is not None:
        row.work_order_created_at = work_order_created_at
    return row


def _local_subscriber(db: Session, body: dict) -> Subscriber | None:
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


def _apply_item(db: Session, sub_uuid, item: dict) -> None:
    crm_work_order_id = str(item.get("id") or "").strip()
    if not crm_work_order_id:
        return
    _upsert_row(
        db,
        subscriber_id=sub_uuid,
        crm_work_order_id=crm_work_order_id,
        title=item.get("title"),
        status=item.get("status"),
        work_type=item.get("work_type"),
        priority=item.get("priority"),
        technician_name=item.get("technician_name"),
        technician_phone=item.get("technician_phone"),
        address=item.get("address"),
        scheduled_start=_to_dt(item.get("scheduled_start")),
        scheduled_end=_to_dt(item.get("scheduled_end")),
        estimated_arrival_at=_to_dt(item.get("estimated_arrival_at")),
        estimated_duration_minutes=_to_int(item.get("estimated_duration_minutes")),
        completed_at=_to_dt(item.get("completed_at")),
        work_order_created_at=_to_dt(item.get("created_at")),
    )


def reconcile_subscriber(db: Session, subscriber_id: str) -> bool:
    """Pull the subscriber's work orders from the CRM into the mirror. Returns
    True on success, False if not CRM-linked. Raises CRMClientError on outage."""
    crm_subscriber_id = resolve_crm_subscriber_id(db, str(subscriber_id))
    if not crm_subscriber_id:
        return False

    data = get_crm_client().get_portal_work_orders(crm_subscriber_id)
    sub_uuid = coerce_uuid(str(subscriber_id))

    for item in data.get("work_orders") or []:
        _apply_item(db, sub_uuid, item)

    sync = db.get(WorkOrderSyncState, sub_uuid)
    if sync is None:
        sync = WorkOrderSyncState(subscriber_id=sub_uuid)
        db.add(sync)
    sync.synced_at = datetime.now(UTC)
    db.commit()
    return True


def reconcile_all(db: Session, *, stale_after_seconds: int = 3600) -> int:
    cutoff = datetime.now(UTC) - timedelta(seconds=max(60, stale_after_seconds))
    stale = db.scalars(
        select(WorkOrderSyncState.subscriber_id).where(
            WorkOrderSyncState.synced_at < cutoff
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
                "work_order_reconcile_failed subscriber=%s: %s", subscriber_id, exc
            )
    return done


def read_for_subscriber(
    db: Session,
    subscriber_id: str,
    *,
    refresh_ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS,
) -> dict:
    """Build the field-service payload from the mirror, lazily refreshing from
    the CRM when the cache is missing or stale (best-effort)."""
    sub_uuid = coerce_uuid(str(subscriber_id))
    sync = db.get(WorkOrderSyncState, sub_uuid)
    cutoff = datetime.now(UTC) - timedelta(seconds=max(0, refresh_ttl_seconds))
    synced = _as_utc(sync.synced_at) if sync else None
    if sync is None or synced is None or synced < cutoff:
        try:
            reconcile_subscriber(db, str(subscriber_id))
        except CRMClientError as exc:
            db.rollback()
            logger.warning(
                "work_order_lazy_refresh_failed subscriber=%s: %s", sub_uuid, exc
            )

    rows = db.scalars(
        select(WorkOrderMirror)
        .where(WorkOrderMirror.subscriber_id == sub_uuid)
        .order_by(WorkOrderMirror.created_at.desc())
    ).all()

    items = [
        {
            "id": r.crm_work_order_id,
            "title": r.title,
            "status": r.status,
            "work_type": r.work_type,
            "priority": r.priority,
            "technician_name": r.technician_name,
            "technician_phone": r.technician_phone,
            "address": r.address,
            "scheduled_start": r.scheduled_start.isoformat()
            if r.scheduled_start
            else None,
            "scheduled_end": r.scheduled_end.isoformat() if r.scheduled_end else None,
            "estimated_arrival_at": r.estimated_arrival_at.isoformat()
            if r.estimated_arrival_at
            else None,
            "estimated_duration_minutes": r.estimated_duration_minutes,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            "created_at": r.work_order_created_at.isoformat()
            if r.work_order_created_at
            else None,
        }
        for r in rows
    ]
    upcoming = sum(
        1 for r in rows if r.status not in ("completed", "canceled", "draft")
    )
    return {"work_orders": items, "total": len(items), "upcoming": upcoming}


_STATUS_EVENTS = {
    "work_order.created",
    "work_order.updated",
    "work_order.dispatched",
    "work_order.completed",
    "work_order.canceled",
}


def apply_webhook(db: Session, event_type: str, body: dict) -> dict:
    """Apply a CRM work-order lifecycle event to the mirror. Carries subscriber +
    fields → upsert; full detail filled by reconcile / lazy refresh."""
    crm_work_order_id = str(body.get("work_order_id") or body.get("id") or "").strip()
    if not crm_work_order_id:
        return {"status": "ignored", "reason": "incomplete_payload"}
    if event_type not in _STATUS_EVENTS:
        return {"status": "ignored", "event": event_type}

    subscriber = _local_subscriber(db, body)
    if subscriber is None:
        logger.warning(
            "crm_work_order_event_unmapped event=%s work_order_id=%s",
            event_type,
            crm_work_order_id,
        )
        return {"status": "ignored", "reason": "unmapped_subscriber"}

    completed_at = None
    if event_type == "work_order.completed":
        completed_at = _to_dt(body.get("completed_at")) or datetime.now(UTC)

    _upsert_row(
        db,
        subscriber_id=subscriber.id,
        crm_work_order_id=crm_work_order_id,
        title=body.get("title"),
        status=body.get("status") or body.get("to_status"),
        work_type=body.get("work_type"),
        priority=body.get("priority"),
        technician_name=body.get("technician_name"),
        technician_phone=body.get("technician_phone"),
        scheduled_start=_to_dt(body.get("scheduled_start")),
        scheduled_end=_to_dt(body.get("scheduled_end")),
        estimated_arrival_at=_to_dt(body.get("estimated_arrival_at")),
        completed_at=completed_at,
    )
    # Mark stale so the next read pulls full detail.
    sync = db.get(WorkOrderSyncState, subscriber.id)
    if sync is not None:
        sync.synced_at = datetime(1970, 1, 1, tzinfo=UTC)
    db.commit()

    if event_type in ("work_order.dispatched", "work_order.completed"):
        try:
            from app.services import push as push_service

            if event_type == "work_order.dispatched":
                title, msg = (
                    "Technician on the way",
                    "Your field-service visit is scheduled.",
                )
            else:
                title, msg = (
                    "Visit completed",
                    "Your field-service work order is complete.",
                )
            push_service.send_push(
                db,
                str(subscriber.id),
                title=title,
                body=msg,
                data={"type": "work_order", "work_order_id": crm_work_order_id},
            )
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            logger.warning(
                "work_order_push_failed work_order_id=%s: %s", crm_work_order_id, exc
            )

    return {"status": "ok", "event": event_type}
