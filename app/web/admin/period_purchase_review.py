"""Finance review adapters for existing purchase and manual outage commands."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import finish_read_transaction, get_db
from app.services.auth_dependencies import has_permission, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.outage_compensation import (
    OUTAGE_APPROVAL_SCOPE,
    ApproveOutageCompensationCommand,
    approve_outage_compensation,
)
from app.services.prepaid_period_purchases import (
    PURCHASE_REPAIR_SCOPE,
    RetryPurchaseSettlementCommand,
    retry_purchase_settlement,
)
from app.services.web_billing_period_reviews import (
    PeriodReviewQuery,
    resolve_period_reviews,
)
from app.web.admin.billing_extensions import _command_context, _context, templates

router = APIRouter(prefix="/billing/service-period-review", tags=["web-admin-billing"])


@router.get("", dependencies=[Depends(require_permission("billing:extension:read"))])
def period_review_page(request: Request, db: Session = Depends(get_db)):
    rows = resolve_period_reviews(db, PeriodReviewQuery())
    auth = getattr(request.state, "auth", {}) or {}
    can_approve = has_permission(auth, db, OUTAGE_APPROVAL_SCOPE)
    can_repair = has_permission(auth, db, PURCHASE_REPAIR_SCOPE)
    data = _context(
        request,
        db,
        {
            "rows": rows,
            "now": datetime.now(UTC),
            "new_key": uuid4,
            "can_approve": can_approve,
            "can_repair": can_repair,
        },
    )
    finish_read_transaction(db)
    return templates.TemplateResponse("admin/billing/period_purchase_review.html", data)


def _principal(request: Request) -> UUID:
    auth = getattr(request.state, "auth", {}) or {}
    if auth.get("principal_type") != "system_user":
        raise HTTPException(
            status_code=403, detail="Named active staff approval is required"
        )
    try:
        return UUID(str(auth.get("principal_id")))
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=403, detail="Staff principal is unavailable"
        ) from exc


@router.post(
    "/outage/{decision_id}/approve",
    dependencies=[Depends(require_permission(OUTAGE_APPROVAL_SCOPE))],
)
def approve_outage_review(
    request: Request,
    decision_id: UUID,
    fingerprint: str = Form(...),
    reason: str = Form(..., min_length=1, max_length=1000),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    principal_id = _principal(request)
    context = _command_context(
        request,
        scope=OUTAGE_APPROVAL_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    db_session_adapter.release_read_transaction(db)
    try:
        approve_outage_compensation(
            db,
            ApproveOutageCompensationCommand(
                decision_id=decision_id,
                expected_fingerprint=fingerprint,
                actor_system_user_id=principal_id,
                effective_at=datetime.now(UTC),
            ),
            context=context,
        )
    except DomainError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return RedirectResponse(url="/admin/billing/service-period-review", status_code=303)


@router.post(
    "/purchase/{purchase_id}/recover",
    dependencies=[Depends(require_permission(PURCHASE_REPAIR_SCOPE))],
)
def recover_purchase_review(
    request: Request,
    purchase_id: UUID,
    fingerprint: str = Form(...),
    reason: str = Form(..., min_length=1, max_length=1000),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    principal_id = _principal(request)
    context = _command_context(
        request,
        scope=PURCHASE_REPAIR_SCOPE,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    db_session_adapter.release_read_transaction(db)
    try:
        retry_purchase_settlement(
            db,
            RetryPurchaseSettlementCommand(
                purchase_id=purchase_id,
                expected_fingerprint=fingerprint,
                actor_system_user_id=principal_id,
                permission_granted=True,
                effective_at=datetime.now(UTC),
            ),
            context=context,
        )
    except DomainError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return RedirectResponse(url="/admin/billing/service-period-review", status_code=303)
