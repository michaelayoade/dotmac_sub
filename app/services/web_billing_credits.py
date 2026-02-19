"""Service helpers for billing credit-note web routes."""

from __future__ import annotations

from uuid import UUID

from app.models.billing import CreditNote, CreditNoteStatus
from app.services import subscriber as subscriber_service
from app.services import web_billing_customers as web_billing_customers_service


def build_credits_list_data(
    db,
    *,
    page: int,
    status: str | None,
    customer_ref: str | None,
) -> dict[str, object]:
    per_page = 50
    offset = (page - 1) * per_page

    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [
            UUID(item["id"])
            for item in web_billing_customers_service.accounts_for_customer(db, customer_ref)
        ]

    if customer_filtered and not account_ids:
        status_counts = {
            "draft": 0,
            "issued": 0,
            "partially_applied": 0,
            "applied": 0,
            "void": 0,
        }
    else:
        status_query = db.query(CreditNote)
        if account_ids:
            status_query = status_query.filter(CreditNote.account_id.in_(account_ids))
        status_counts = {
            "draft": status_query.filter(CreditNote.status == CreditNoteStatus.draft).count(),
            "issued": status_query.filter(CreditNote.status == CreditNoteStatus.issued).count(),
            "partially_applied": status_query.filter(CreditNote.status == CreditNoteStatus.partially_applied).count(),
            "applied": status_query.filter(CreditNote.status == CreditNoteStatus.applied).count(),
            "void": status_query.filter(CreditNote.status == CreditNoteStatus.void).count(),
        }

    query = db.query(CreditNote).filter(CreditNote.is_active.is_(True))
    credits = []
    total = 0
    total_pages = 1
    if account_ids:
        query = query.filter(CreditNote.account_id.in_(account_ids))
    if not customer_filtered or account_ids:
        if status:
            query = query.filter(CreditNote.status == status)
        total = query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        credits = query.order_by(CreditNote.created_at.desc()).offset(offset).limit(per_page).all()

    return {
        "credits": credits,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": status,
        "status_counts": status_counts,
        "customer_ref": customer_ref,
    }


def resolve_selected_account(db, account_id: str | None):
    if not account_id:
        return None
    try:
        return subscriber_service.accounts.get(db=db, account_id=account_id)
    except Exception:
        return None
