"""Admin billing credit note routes."""

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_credits as web_billing_credits_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


@router.get(
    "/credits",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:credit_note:read"))],
)
def billing_credits_list(
    request: Request,
    page: int = 1,
    per_page: int = Query(50, ge=10, le=100),
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_billing_credits_service.build_credits_list_data(
        db,
        page=page,
        per_page=per_page,
        status=status,
        customer_ref=customer_ref,
    )
    return templates.TemplateResponse(
        "admin/billing/credits.html",
        {
            "request": request,
            **state,
            "active_page": "credits",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get(
    "/credits/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:credit_note:create"))],
)
def billing_credit_new(
    request: Request,
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/credit_form.html",
        {
            "request": request,
            **web_billing_credits_service.credit_form_context(
                db,
                account_id=account_id or account,
            ),
            "active_page": "credits",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/credits",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:credit_note:create"))],
)
def billing_credit_create(
    request: Request,
    account_id: str = Form(...),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        web_billing_credits_service.create_credit_from_form(
            db,
            account_id=account_id,
            amount=amount,
            currency=currency,
            memo=memo,
        )
    except Exception as exc:
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/credit_form.html",
            {
                "request": request,
                **web_billing_credits_service.credit_form_context(
                    db,
                    account_id=account_id,
                    error=str(exc),
                ),
                "active_page": "credits",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/credits", status_code=303)
