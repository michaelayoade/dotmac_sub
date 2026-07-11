"""Reseller-facing aggregations of the CRM mirrors (Sales/Quotes B3).

A reseller manages many customer accounts (``Subscriber.reseller_id``). These
helpers aggregate the per-subscriber quotes / projects / work orders across the
reseller's whole customer set, tagging each row with its account so the
reseller can see "which customer".

Phase 3 (§4.2): the quote and project reads run behind the per-vertical
``{quotes,projects}_native_read_enabled`` read-flip flags — OFF (default)
serves the local CRM mirrors (the reconcile task keeps them fresh, so a
reseller dashboard never fans out N CRM calls and works during a CRM outage),
ON serves sub's native tables. Response shells and item shapes are identical
(§2.5). Work orders stay mirror-only until the Phase 2 flip.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.project import Project
from app.models.project_mirror import ProjectMirror
from app.models.quote_mirror import QuoteMirror
from app.models.work_order_mirror import WorkOrderMirror
from app.services import projects as projects_service
from app.services import quotes_mirror, reseller_portal
from app.services.sales import selfserve as selfserve_service
from app.services.work_order_views import row_to_item

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

    if selfserve_service.native_read_enabled(db):
        quotes = selfserve_service.selfserve_quotes.list_for_subscribers(
            db, [str(s) for s in names]
        )
        # H1: batch-resolve install-project ids for the whole subtree in one
        # query instead of a per-quote metadata scan.
        project_ids = selfserve_service._find_project_ids_for_quotes(
            db, [q.id for q in quotes]
        )
        native_items: list[dict] = []
        for q in quotes:
            item = selfserve_service.build_portal_quote_payload(
                db, q, project_id=project_ids.get(str(q.id))
            )
            item["account_id"] = str(q.subscriber_id)
            item["account_name"] = names.get(q.subscriber_id)
            native_items.append(item)
        open_count = sum(
            1
            for i in native_items
            if i["status"] not in selfserve_service._PORTAL_CLOSED_QUOTE_STATUSES
        )
        return {"quotes": native_items, "total": len(native_items), "open": open_count}

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


# Reseller project rows carry this subset of the portal payload keys (§2.5:
# the `{projects,total,active}` shell + account tags are the contract).
_RESELLER_PROJECT_KEYS = (
    "id",
    "name",
    "status",
    "project_type",
    "progress_pct",
    "current_stage",
    "region",
    "customer_address",
    "due_at",
    "created_at",
)


def projects_for_reseller(db: Session, reseller_id: str) -> dict:
    names = _customer_names(db, reseller_id)
    if not names:
        return {"projects": [], "total": 0, "active": 0}

    if projects_service.native_read_enabled(db):
        native_rows = (
            db.query(Project)
            .options(selectinload(Project.tasks))
            .filter(Project.subscriber_id.in_(list(names)))
            .filter(Project.is_active.is_(True))
            .order_by(Project.created_at.desc())
            .all()
        )
        native_items = []
        for p in native_rows:
            payload = projects_service.build_portal_project_payload(p)
            native_items.append(
                {
                    "account_id": str(p.subscriber_id),
                    "account_name": names.get(p.subscriber_id),
                    **{key: payload[key] for key in _RESELLER_PROJECT_KEYS},
                }
            )
        active = sum(
            1
            for i in native_items
            if i["status"] not in ("completed", "canceled", "closed")
        )
        return {
            "projects": native_items,
            "total": len(native_items),
            "active": active,
        }

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
    items = []
    for r in rows:
        item = row_to_item(r, include_internal=False)
        item["account_id"] = str(r.subscriber_id)
        item["account_name"] = names.get(r.subscriber_id)
        items.append(item)
    upcoming = sum(
        1 for r in rows if r.status not in ("completed", "canceled", "draft")
    )
    return {"work_orders": items, "total": len(items), "upcoming": upcoming}
