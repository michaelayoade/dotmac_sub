"""Admin web routes for consolidated reseller billing."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import display_format
from app.services import web_consolidated_billing as web_consolidated_billing_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])

_CONSOLIDATED_PAYMENT_ERROR = (
    "Unable to record consolidated payment. Check the amount and account details."
)
_CONSOLIDATED_PAYMENT_SUCCESS = "Consolidated payment recorded and distributed."


def _default_currency(db: Session) -> str:
    return display_format.default_currency(db)


def _consolidated_detail_url(
    billing_account_id: str,
    *,
    payment_note: str | None = None,
    payment_error: str | None = None,
) -> str:
    params = {
        key: value
        for key, value in {
            "payment_note": payment_note,
            "payment_error": payment_error,
        }.items()
        if value
    }
    query = urlencode(params)
    url = f"/admin/billing/consolidated-accounts/{billing_account_id}"
    if query:
        return f"{url}?{query}"
    return url


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
    currency: str | None = Form(None),
    memo: str | None = Form(None),
    collection_account_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        web_consolidated_billing_service.record_bulk_payment(
            db,
            billing_account_id=billing_account_id,
            amount=amount,
            currency=(currency or _default_currency(db)).strip().upper(),
            memo=memo,
            collection_account_id=collection_account_id or None,
        )
    except Exception:
        db.rollback()
        return RedirectResponse(
            url=_consolidated_detail_url(
                billing_account_id,
                payment_error=_CONSOLIDATED_PAYMENT_ERROR,
            ),
            status_code=303,
        )
    return RedirectResponse(
        url=_consolidated_detail_url(
            billing_account_id,
            payment_note=_CONSOLIDATED_PAYMENT_SUCCESS,
        ),
        status_code=303,
    )
