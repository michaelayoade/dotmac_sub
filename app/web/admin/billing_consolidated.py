"""Admin web routes for consolidated reseller billing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_consolidated_billing as web_consolidated_billing_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


@router.get(
    "/consolidated-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing_account:read"))],
)
def consolidated_accounts_list(
    request: Request,
    reseller_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=200),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    context = web_consolidated_billing_service.build_list_context(
        db, reseller_id=reseller_id, page=page, per_page=per_page
    )
    return templates.TemplateResponse(
        "admin/billing/consolidated/index.html",
        {
            "request": request,
            **context,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/consolidated-accounts/{billing_account_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing_account:read"))],
)
def consolidated_account_detail(
    request: Request,
    billing_account_id: str,
    subscribers_page: int = Query(1, ge=1, alias="subs_page"),
    payments_page: int = Query(1, ge=1, alias="pay_page"),
    subscribers_per_page: int = Query(25, ge=10, le=200, alias="subs_per_page"),
    payments_per_page: int = Query(25, ge=10, le=200, alias="pay_per_page"),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    context = web_consolidated_billing_service.build_detail_context(
        db,
        billing_account_id,
        subscribers_page=subscribers_page,
        payments_page=payments_page,
        subscribers_per_page=subscribers_per_page,
        payments_per_page=payments_per_page,
    )
    return templates.TemplateResponse(
        "admin/billing/consolidated/detail.html",
        {
            "request": request,
            **context,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/consolidated-accounts/{billing_account_id}/record-payment",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing_account:distribute"))],
)
def consolidated_record_payment(
    request: Request,
    billing_account_id: str,
    amount: str = Form(...),
    currency: str = Form("NGN"),
    memo: str | None = Form(None),
    collection_account_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    web_consolidated_billing_service.record_bulk_payment(
        db,
        billing_account_id=billing_account_id,
        amount=amount,
        currency=currency,
        memo=memo,
        collection_account_id=collection_account_id or None,
    )
    return RedirectResponse(
        url=f"/admin/billing/consolidated-accounts/{billing_account_id}",
        status_code=303,
    )
