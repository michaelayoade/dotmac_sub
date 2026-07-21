"""Reseller-facing aggregations of native Sub work and quote projections.

A reseller manages many customer accounts (``Subscriber.reseller_id``). These
helpers aggregate the per-subscriber quotes / projects / work orders across the
reseller's whole customer set, tagging each row with its account so the
reseller can see "which customer".

Projects and work orders are native Sub reads. Quotes retain their independent
sales cutover contract.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.quote_mirror import QuoteMirror
from app.services import customer_experience_lifecycle, quotes_mirror, reseller_portal
from app.services.sales import selfserve as selfserve_service

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

    items = []
    for subscriber_id, account_name in names.items():
        response = customer_experience_lifecycle.projects_for_subscriber(
            db, str(subscriber_id)
        )
        for project in response.projects:
            payload = project.model_dump(mode="json")
            items.append(
                {
                    "account_id": str(subscriber_id),
                    "account_name": account_name,
                    **{key: payload[key] for key in _RESELLER_PROJECT_KEYS},
                }
            )
    active = sum(
        1 for item in items if item["status"] not in ("completed", "canceled", "closed")
    )
    return {"projects": items, "total": len(items), "active": active}


def work_orders_for_reseller(db: Session, reseller_id: str) -> dict:
    names = _customer_names(db, reseller_id)
    if not names:
        return {"work_orders": [], "total": 0, "upcoming": 0}
    items = []
    upcoming = 0
    for subscriber_id, account_name in names.items():
        response = customer_experience_lifecycle.work_orders_for_subscriber(
            db, str(subscriber_id)
        )
        upcoming += response.upcoming
        for work_order in response.work_orders:
            items.append(
                {
                    "account_id": str(subscriber_id),
                    "account_name": account_name,
                    **work_order.model_dump(mode="json"),
                }
            )
    return {"work_orders": items, "total": len(items), "upcoming": upcoming}
