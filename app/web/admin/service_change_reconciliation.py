"""Thin operator adapter for interrupted service-change execution chains."""

from __future__ import annotations

from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_admin as web_admin_service
from app.services.auth_dependencies import require_permission
from app.services.subscription_change_execution import (
    SubscriptionChangeExecutionError,
    inspect_execution_chain_reconciliation,
    reconcile_execution_chain,
)

router = APIRouter(
    prefix="/provisioning/service-change-reconciliation",
    tags=["web-admin-service-change-reconciliation"],
)
templates = Jinja2Templates(directory="templates")
PERMISSION = "provisioning:service_change_reconcile"


@router.get(
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission(PERMISSION))],
)
def service_change_reconciliation_page(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    from app.web.admin import get_current_user, get_sidebar_stats

    inspection = inspect_execution_chain_reconciliation(db, limit=limit)
    return templates.TemplateResponse(
        "admin/provisioning/service_change_reconciliation.html",
        {
            "request": request,
            "active_page": "provisioning",
            "active_menu": "services",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "inspection": inspection,
        },
    )


@router.post(
    "/{request_id}/repair",
    dependencies=[Depends(require_permission(PERMISSION))],
)
def repair_service_change_execution(
    request: Request,
    request_id: UUID,
    expected_head: str = Form(..., min_length=64, max_length=64),
    idempotency_key: str = Form(..., min_length=16, max_length=160),
    reason: str = Form(..., min_length=8, max_length=500),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    actor_id = str(web_admin_service.get_actor_id(request) or "admin")
    try:
        reconcile_execution_chain(
            db,
            request_id=request_id,
            expected_head=expected_head,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            reason=reason,
        )
        status, message = (
            "success",
            "Execution chain reconciled from canonical evidence.",
        )
    except SubscriptionChangeExecutionError as exc:
        status, message = "error", str(exc)
    return RedirectResponse(
        url=(
            "/admin/provisioning/service-change-reconciliation"
            f"?feedback_status={quote_plus(status)}"
            f"&feedback_message={quote_plus(message)}"
        ),
        status_code=303,
    )
