"""Admin billing payment arrangements routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import payment_arrangements as payment_arrangements_service
from app.services import web_billing_arrangements as web_billing_arrangements_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


@router.get(
    "/payment-arrangements",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_arrangements_list(
    request: Request,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    state = web_billing_arrangements_service.list_data(
        db,
        status=status,
        page=page,
        per_page=per_page,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_arrangements.html",
        {
            "request": request,
            **state,
            "active_page": "payment_arrangements",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/payment-arrangements/{arrangement_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_arrangements_detail(
    request: Request,
    arrangement_id: UUID,
    db: Session = Depends(get_db),
):
    state = web_billing_arrangements_service.detail_data(
        db, arrangement_id=str(arrangement_id)
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment arrangement not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/payment_arrangement_detail.html",
        {
            "request": request,
            **state,
            "active_page": "payment_arrangements",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-arrangements/{arrangement_id}/approve",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_arrangements_approve(
    arrangement_id: UUID,
    db: Session = Depends(get_db),
):
    payment_arrangements_service.payment_arrangements.approve(
        db, str(arrangement_id)
    )
    return RedirectResponse(
        url=f"/admin/billing/payment-arrangements/{arrangement_id}",
        status_code=303,
    )


@router.post(
    "/payment-arrangements/{arrangement_id}/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_arrangements_cancel(
    arrangement_id: UUID,
    db: Session = Depends(get_db),
):
    payment_arrangements_service.payment_arrangements.cancel(
        db, str(arrangement_id)
    )
    return RedirectResponse(
        url=f"/admin/billing/payment-arrangements/{arrangement_id}",
        status_code=303,
    )
