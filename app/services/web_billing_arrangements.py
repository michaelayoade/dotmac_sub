"""Service helpers for billing payment-arrangement web routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.payment_arrangement import (
    ArrangementStatus,
    PaymentArrangement,
    PaymentFrequency,
)
from app.services import payment_arrangements as arrangement_service
from app.services.audit_helpers import log_audit_event
from app.services.common import validate_enum as _validate_enum

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _actor_id(request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    actor = current_user.get("id") if current_user else None
    return str(actor) if actor else None


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
    stmt = select(func.count(PaymentArrangement.id)).where(
        PaymentArrangement.is_active.is_(True)
    )
    if status:
        stmt = stmt.where(
            PaymentArrangement.status
            == _validate_enum(status, ArrangementStatus, "status")
        )
    total = db.scalar(stmt) or 0
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

    # The "record payment" action only applies to in-progress arrangements,
    # and only to the next unpaid installment.
    next_actionable_id = None
    if arrangement.status in (ArrangementStatus.active, ArrangementStatus.defaulted):
        next_actionable = arrangement_service.get_next_actionable_installment(
            db, arrangement_id
        )
        next_actionable_id = str(next_actionable.id) if next_actionable else None

    return {
        "arrangement": arrangement,
        "installments": installments,
        "next_actionable_installment_id": next_actionable_id,
        "statuses": [s.value for s in ArrangementStatus],
        "frequencies": [f.value for f in PaymentFrequency],
    }


def approve_arrangement(db: Session, request, *, arrangement_id: str):
    arrangement = arrangement_service.payment_arrangements.approve(
        db,
        arrangement_id,
        approved_by_user_id=_actor_id(request),
    )
    log_audit_event(
        db=db,
        request=request,
        action="approve",
        entity_type="payment_arrangement",
        entity_id=str(arrangement.id),
        actor_id=_actor_id(request),
        metadata={
            "subscriber_id": str(arrangement.subscriber_id),
            "total_amount": str(arrangement.total_amount),
            "installments_total": arrangement.installments_total,
        },
    )
    return arrangement


def cancel_arrangement(db: Session, *, arrangement_id: str):
    return arrangement_service.payment_arrangements.cancel(db, arrangement_id)


def record_installment_payment(
    db: Session,
    request,
    *,
    arrangement_id: str,
    note: str | None = None,
):
    """Record payment for the arrangement's next due/overdue installment."""
    arrangement = arrangement_service.payment_arrangements.get(db, arrangement_id)
    installment = arrangement_service.get_next_actionable_installment(
        db, arrangement_id
    )
    if installment is None:
        raise HTTPException(
            status_code=400, detail="No unpaid installment to record payment for"
        )
    installment = arrangement_service.payment_arrangements.record_installment_payment(
        db,
        str(installment.id),
        notes=note,
    )
    log_audit_event(
        db=db,
        request=request,
        action="record_installment_payment",
        entity_type="payment_arrangement",
        entity_id=str(arrangement.id),
        actor_id=_actor_id(request),
        metadata={
            "installment_id": str(installment.id),
            "installment_number": installment.installment_number,
            "amount": str(installment.amount),
            "note": note or "",
        },
    )
    return installment
