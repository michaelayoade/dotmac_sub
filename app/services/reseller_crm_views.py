"""Reseller-facing aggregations of the CRM mirrors (Sales/Quotes B3).

A reseller manages many customer accounts (``Subscriber.reseller_id``). These
helpers aggregate the per-subscriber CRM mirrors — quotes, projects, work orders —
across the reseller's whole customer set, tagging each row with its account so the
reseller can see "which customer". Reads come straight from the local mirror (the
reconcile task keeps it fresh), so a reseller dashboard never fans out N CRM calls
and works during a CRM outage.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project_mirror import ProjectMirror
from app.models.quote_mirror import QuoteMirror
from app.models.work_order_mirror import WorkOrderMirror
from app.services import quotes_mirror, reseller_portal

logger = logging.getLogger(__name__)


def _customer_names(db: Session, reseller_id: str) -> dict:
    """Map each of the reseller's customer subscriber ids → a display name."""
    subs = reseller_portal._customer_accounts_query(db, reseller_id).all()
    names: dict = {}
    for s in subs:
        full = f"{s.first_name or ''} {s.last_name or ''}".strip()
        names[s.id] = s.company_name or full or s.email
    return names


def _dt(value) -> str | None:
    return value.isoformat() if value else None


def quotes_for_reseller(db: Session, reseller_id: str) -> dict:
    names = _customer_names(db, reseller_id)
    if not names:
        return {"quotes": [], "total": 0, "open": 0}
    rows = db.scalars(
        select(QuoteMirror)
        .where(QuoteMirror.subscriber_id.in_(list(names)))
        .order_by(QuoteMirror.created_at.desc())
    ).all()
    items: list[dict] = []
    for r in rows:
        item = quotes_mirror._row_to_item(r)
        item["account_id"] = str(r.subscriber_id)
        item["account_name"] = names.get(r.subscriber_id)
        items.append(item)
    open_count = sum(
        1 for r in rows if r.status not in ("accepted", "rejected", "expired")
    )
    return {"quotes": items, "total": len(items), "open": open_count}


def projects_for_reseller(db: Session, reseller_id: str) -> dict:
    names = _customer_names(db, reseller_id)
    if not names:
        return {"projects": [], "total": 0, "active": 0}
    rows = db.scalars(
        select(ProjectMirror)
        .where(ProjectMirror.subscriber_id.in_(list(names)))
        .order_by(ProjectMirror.created_at.desc())
    ).all()
    items = [
        {
            "account_id": str(r.subscriber_id),
            "account_name": names.get(r.subscriber_id),
            "id": r.crm_project_id,
            "name": r.name,
            "status": r.status,
            "project_type": r.project_type,
            "progress_pct": r.progress_pct,
            "current_stage": r.current_stage,
            "region": r.region,
            "customer_address": r.customer_address,
            "due_at": _dt(r.due_at),
            "created_at": _dt(r.project_created_at),
        }
        for r in rows
    ]
    active = sum(1 for r in rows if r.status not in ("completed", "canceled", "closed"))
    return {"projects": items, "total": len(items), "active": active}


def work_orders_for_reseller(db: Session, reseller_id: str) -> dict:
    names = _customer_names(db, reseller_id)
    if not names:
        return {"work_orders": [], "total": 0, "upcoming": 0}
    rows = db.scalars(
        select(WorkOrderMirror)
        .where(WorkOrderMirror.subscriber_id.in_(list(names)))
        .order_by(WorkOrderMirror.created_at.desc())
    ).all()
    items = [
        {
            "account_id": str(r.subscriber_id),
            "account_name": names.get(r.subscriber_id),
            "id": r.crm_work_order_id,
            "title": r.title,
            "status": r.status,
            "work_type": r.work_type,
            "priority": r.priority,
            "technician_name": r.technician_name,
            "technician_phone": r.technician_phone,
            "address": r.address,
            "scheduled_start": _dt(r.scheduled_start),
            "estimated_arrival_at": _dt(r.estimated_arrival_at),
            "completed_at": _dt(r.completed_at),
        }
        for r in rows
    ]
    upcoming = sum(
        1 for r in rows if r.status not in ("completed", "canceled", "draft")
    )
    return {"work_orders": items, "total": len(items), "upcoming": upcoming}
