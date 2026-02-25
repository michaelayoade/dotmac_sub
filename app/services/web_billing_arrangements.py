"""Service helpers for billing payment-arrangement web routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.payment_arrangement import ArrangementStatus, PaymentFrequency
from app.services import payment_arrangements as arrangement_service

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def list_data(
    db: Session,
    *,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Build template context for the payment arrangements list page."""
    offset = (page - 1) * per_page
    arrangements = arrangement_service.payment_arrangements.list(
        db=db,
        account_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    # Count all for pagination
    all_arrangements = arrangement_service.payment_arrangements.list(
        db=db,
        account_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_arrangements)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return {
        "arrangements": arrangements,
        "statuses": [s.value for s in ArrangementStatus],
        "frequencies": [f.value for f in PaymentFrequency],
        "status_filter": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def detail_data(db: Session, *, arrangement_id: str) -> dict[str, object] | None:
    """Build template context for the payment arrangement detail page."""
    arrangement = arrangement_service.payment_arrangements.get(db, arrangement_id)
    if not arrangement:
        return None

    installments = arrangement_service.installments.list(
        db=db,
        arrangement_id=arrangement_id,
        status=None,
        order_by="installment_number",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    return {
        "arrangement": arrangement,
        "installments": installments,
        "statuses": [s.value for s in ArrangementStatus],
        "frequencies": [f.value for f in PaymentFrequency],
    }
