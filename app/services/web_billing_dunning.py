"""Service helpers for billing dunning web routes."""

from __future__ import annotations

from app.models.collections import DunningCase, DunningCaseStatus
from app.services import collections as collections_service
from app.services import web_billing_customers as web_billing_customers_service


def build_listing_data(
    db,
    *,
    page: int,
    status: str | None,
    customer_ref: str | None,
) -> dict[str, object]:
    """Build paginated listing data and status counts for dunning page."""
    per_page = 50
    offset = (page - 1) * per_page

    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [
            item["id"]
            for item in web_billing_customers_service.accounts_for_customer(db, customer_ref)
        ]

    if customer_filtered and not account_ids:
        status_counts = {
            "open": 0,
            "paused": 0,
            "resolved": 0,
            "closed": 0,
        }
    else:
        status_query = db.query(DunningCase)
        if account_ids:
            status_query = status_query.filter(DunningCase.account_id.in_(account_ids))
        status_counts = {
            "open": status_query.filter(DunningCase.status == DunningCaseStatus.open).count(),
            "paused": status_query.filter(DunningCase.status == DunningCaseStatus.paused).count(),
            "resolved": status_query.filter(DunningCase.status == DunningCaseStatus.resolved).count(),
            "closed": status_query.filter(DunningCase.status == DunningCaseStatus.closed).count(),
        }

    cases = []
    total = 0
    total_pages = 1
    if account_ids:
        query = db.query(DunningCase).filter(DunningCase.account_id.in_(account_ids))
        if status:
            query = query.filter(DunningCase.status == status)
        total = query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        cases = query.order_by(DunningCase.created_at.desc()).offset(offset).limit(per_page).all()
    elif not customer_filtered:
        count_query = db.query(DunningCase)
        if status:
            count_query = count_query.filter(DunningCase.status == status)
        total = count_query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        cases = collections_service.dunning_cases.list(
            db=db,
            account_id=None,
            status=status,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )

    return {
        "cases": cases,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": status,
        "status_counts": status_counts,
        "customer_ref": customer_ref,
    }


def apply_case_action(db, *, case_id: str, action: str) -> None:
    """Apply a single dunning-case action."""
    if action == "pause":
        collections_service.dunning_cases.pause(db=db, case_id=case_id)
        return
    if action == "resume":
        collections_service.dunning_cases.resume(db=db, case_id=case_id)
        return
    if action == "close":
        collections_service.dunning_cases.close(db=db, case_id=case_id)
        return
    raise ValueError("Unsupported action")


def apply_bulk_action(db, *, case_ids_csv: str, action: str) -> list[str]:
    """Apply dunning action for many IDs; return IDs successfully processed."""
    processed: list[str] = []
    for case_id in [item.strip() for item in case_ids_csv.split(",") if item.strip()]:
        try:
            apply_case_action(db, case_id=case_id, action=action)
            processed.append(case_id)
        except Exception:
            continue
    return processed
