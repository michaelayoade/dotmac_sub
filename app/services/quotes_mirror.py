"""Local mirror of CRM self-serve quote data (Sales/Quotes tracker).

All DB + CRM access for the customer-facing quote flow lives here so the API/web
wrappers stay thin. The CRM owns quotes; this keeps a read-optimised local copy
hydrated by CRM ``quote.*`` webhooks + a periodic reconcile pull + lazy on-view
refresh, plus the write-through that requests a new map-pinned quote. The
estimate/feasibility/deposit are computed by the CRM; this is a faithful copy.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.quote_mirror import QuoteMirror, QuoteSyncState
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError, get_crm_client
from app.services.crm_portal import resolve_crm_subscriber_id

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_TTL_SECONDS = 300  # quotes change slowly; refresh on view


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


def _to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _to_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _upsert_row(db: Session, *, subscriber_id, item: dict) -> QuoteMirror | None:
    crm_quote_id = str(item.get("id") or "").strip()
    if not crm_quote_id:
        return None
    row = db.scalar(select(QuoteMirror).where(QuoteMirror.crm_quote_id == crm_quote_id))
    if row is None:
        row = QuoteMirror(crm_quote_id=crm_quote_id, subscriber_id=subscriber_id)
        db.add(row)
    row.subscriber_id = subscriber_id
    feasibility_raw = item.get("feasibility")
    feasibility = feasibility_raw if isinstance(feasibility_raw, dict) else {}
    if item.get("status"):
        row.status = str(item["status"])
    if item.get("currency"):
        row.currency = str(item["currency"])
    if item.get("total") is not None:
        row.total = str(item["total"])
    if item.get("deposit_amount") is not None:
        row.deposit_amount = str(item["deposit_amount"])
    if item.get("deposit_percent") is not None:
        row.deposit_percent = _to_int(item.get("deposit_percent"))
    if item.get("deposit_paid") is not None:
        row.deposit_paid = bool(item["deposit_paid"])
    if feasibility.get("coverage") is not None:
        row.feasibility_coverage = str(feasibility["coverage"])
    if item.get("estimate_provisional") is not None:
        row.estimate_provisional = bool(item["estimate_provisional"])
    if item.get("address") is not None:
        row.address = str(item["address"])
    if item.get("latitude") is not None:
        row.latitude = _to_float(item.get("latitude"))
    if item.get("longitude") is not None:
        row.longitude = _to_float(item.get("longitude"))
    if item.get("project_id") is not None:
        row.project_id = str(item["project_id"])
    if item.get("sales_order_id") is not None:
        row.sales_order_id = str(item["sales_order_id"])
    if item.get("created_at") is not None:
        row.quote_created_at = _to_dt(item.get("created_at"))
    # Keep the full CRM payload for rich rendering (line items, feasibility detail).
    row.payload = item
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


def reconcile_subscriber(db: Session, subscriber_id: str) -> bool:
    """Pull the subscriber's quotes from the CRM into the mirror. Returns True on
    success, False if not CRM-linked. Raises CRMClientError on outage."""
    crm_subscriber_id = resolve_crm_subscriber_id(db, str(subscriber_id))
    if not crm_subscriber_id:
        return False

    data = get_crm_client().get_portal_quotes(crm_subscriber_id)
    sub_uuid = coerce_uuid(str(subscriber_id))

    for item in data.get("quotes") or []:
        if isinstance(item, dict):
            _upsert_row(db, subscriber_id=sub_uuid, item=item)

    sync = db.get(QuoteSyncState, sub_uuid)
    if sync is None:
        sync = QuoteSyncState(subscriber_id=sub_uuid)
        db.add(sync)
    sync.synced_at = datetime.now(UTC)
    db.commit()
    return True


def reconcile_all(db: Session, *, stale_after_seconds: int = 3600) -> int:
    cutoff = datetime.now(UTC) - timedelta(seconds=max(60, stale_after_seconds))
    stale = db.scalars(
        select(QuoteSyncState.subscriber_id).where(QuoteSyncState.synced_at < cutoff)
    ).all()
    done = 0
    for subscriber_id in stale:
        try:
            if reconcile_subscriber(db, str(subscriber_id)):
                done += 1
        except CRMClientError as exc:
            db.rollback()
            logger.warning(
                "quote_reconcile_failed subscriber=%s: %s", subscriber_id, exc
            )
    return done


def _row_to_item(row: QuoteMirror) -> dict:
    if isinstance(row.payload, dict) and row.payload:
        item = dict(row.payload)
        item["id"] = row.crm_quote_id
        return item
    # Fallback shape from columns if the full payload was never stored.
    return {
        "id": row.crm_quote_id,
        "status": row.status,
        "currency": row.currency,
        "total": row.total,
        "deposit_amount": row.deposit_amount,
        "deposit_percent": row.deposit_percent,
        "deposit_paid": row.deposit_paid,
        "feasibility": {"coverage": row.feasibility_coverage},
        "estimate_provisional": row.estimate_provisional,
        "address": row.address,
        "latitude": row.latitude,
        "longitude": row.longitude,
        "project_id": row.project_id,
        "sales_order_id": row.sales_order_id,
        "created_at": row.quote_created_at.isoformat()
        if row.quote_created_at
        else None,
    }


def _enqueue_lazy_refresh(subscriber_id: str) -> None:
    """Enqueue a background mirror refresh (best-effort — the periodic reconcile
    is the backstop, so an enqueue failure must not break the read)."""
    from app.services.queue_adapter import enqueue_task

    try:
        enqueue_task(
            "app.tasks.quotes.refresh_quote_mirror_for_subscriber",
            args=[subscriber_id],
            source="quote_lazy_refresh",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "quote_lazy_refresh_enqueue_failed subscriber=%s: %s", subscriber_id, exc
        )


def read_for_subscriber(
    db: Session,
    subscriber_id: str,
    *,
    refresh_ttl_seconds: int = _DEFAULT_REFRESH_TTL_SECONDS,
) -> dict:
    """Build the quotes payload from the mirror, lazily refreshing from the CRM
    when the cache is missing or stale (best-effort)."""
    sub_uuid = coerce_uuid(str(subscriber_id))
    sync = db.get(QuoteSyncState, sub_uuid)
    cutoff = datetime.now(UTC) - timedelta(seconds=max(0, refresh_ttl_seconds))
    synced = _as_utc(sync.synced_at) if sync else None
    if sync is None or synced is None:
        # Cold cache — fetch synchronously so the first load is populated.
        try:
            reconcile_subscriber(db, str(subscriber_id))
        except CRMClientError as exc:
            db.rollback()
            logger.warning("quote_lazy_refresh_failed subscriber=%s: %s", sub_uuid, exc)
    elif synced < cutoff:
        # Warm but stale — serve the stale copy now and refresh in the background.
        # Optimistically stamp synced_at so concurrent reads within the TTL don't
        # each enqueue (debounce); the refresh task re-stamps after pulling.
        sync.synced_at = datetime.now(UTC)
        db.commit()
        _enqueue_lazy_refresh(str(subscriber_id))

    rows = db.scalars(
        select(QuoteMirror)
        .where(QuoteMirror.subscriber_id == sub_uuid)
        .order_by(QuoteMirror.created_at.desc())
    ).all()
    items = [_row_to_item(r) for r in rows]
    open_count = sum(
        1 for r in rows if r.status not in ("accepted", "rejected", "expired")
    )
    return {"quotes": items, "total": len(items), "open": open_count}


_STATUS_EVENTS = {
    "quote.created",
    "quote.updated",
    "quote.accepted",
    "quote.rejected",
}


def apply_webhook(db: Session, event_type: str, body: dict) -> dict:
    """Apply a CRM quote lifecycle event to the mirror. The webhook carries a
    lightweight payload → upsert + mark stale so the next read pulls full detail."""
    crm_quote_id = str(body.get("quote_id") or body.get("id") or "").strip()
    if not crm_quote_id:
        return {"status": "ignored", "reason": "incomplete_payload"}
    if event_type not in _STATUS_EVENTS:
        return {"status": "ignored", "event": event_type}

    subscriber = _local_subscriber(db, body)
    if subscriber is None:
        logger.warning(
            "crm_quote_event_unmapped event=%s quote_id=%s", event_type, crm_quote_id
        )
        return {"status": "ignored", "reason": "unmapped_subscriber"}

    item = dict(body)
    item["id"] = crm_quote_id
    _upsert_row(db, subscriber_id=subscriber.id, item=item)
    # Mark stale so the next read pulls full detail from the CRM.
    sync = db.get(QuoteSyncState, subscriber.id)
    if sync is not None:
        sync.synced_at = datetime(1970, 1, 1, tzinfo=UTC)
    db.commit()

    if event_type == "quote.accepted":
        try:
            from app.services import push as push_service

            push_service.send_push(
                db,
                str(subscriber.id),
                title="Quote accepted",
                body="Your installation is being scheduled.",
                data={"type": "quote", "quote_id": crm_quote_id},
            )
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            logger.warning("quote_push_failed quote_id=%s: %s", crm_quote_id, exc)

    return {"status": "ok", "event": event_type}
