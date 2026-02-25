"""Admin billing credit note routes."""

from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import CreditNoteStatus
from app.schemas.billing import CreditNoteCreate
from app.services import billing as billing_service
from app.services import web_billing_credits as web_billing_credits_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _parse_uuid(value: str | None, field: str):
    if not value:
        raise ValueError(f"{field} is required")
    return UUID(value)


def _parse_decimal(value: str | None, field: str, default: Decimal | None = None) -> Decimal:
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a valid number") from exc


@router.get("/credits", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_credits_list(
    request: Request,
    page: int = 1,
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_billing_credits_service.build_credits_list_data(
        db,
        page=page,
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


@router.get("/credits/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def billing_credit_new(
    request: Request,
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    db: Session = Depends(get_db),
):
    resolved_account_id = account_id or account
    selected_account = web_billing_credits_service.resolve_selected_account(
        db,
        resolved_account_id,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/credit_form.html",
        {
            "request": request,
            "accounts": None,
            "action_url": "/admin/billing/credits",
            "form_title": "Issue Credit",
            "submit_label": "Issue Credit",
            "active_page": "credits",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": bool(selected_account),
            "account_label": web_billing_customers_service.account_label(selected_account) if selected_account else None,
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else None,
        },
    )


@router.post("/credits", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def billing_credit_create(
    request: Request,
    account_id: str = Form(...),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        credit_amount = _parse_decimal(amount, "amount")
        payload = CreditNoteCreate(
            account_id=_parse_uuid(account_id, "account_id"),
            status=CreditNoteStatus.issued,
            currency=currency.strip().upper(),
            subtotal=credit_amount,
            tax_total=Decimal("0.00"),
            total=credit_amount,
            memo=memo.strip() if memo else None,
        )
        billing_service.credit_notes.create(db, payload)
    except Exception as exc:
        selected_account = web_billing_credits_service.resolve_selected_account(db, account_id)
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/credit_form.html",
            {
                "request": request,
                "accounts": None,
                "action_url": "/admin/billing/credits",
                "form_title": "Issue Credit",
                "submit_label": "Issue Credit",
                "error": str(exc),
                "active_page": "credits",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "account_locked": bool(selected_account),
                "account_label": web_billing_customers_service.account_label(selected_account) if selected_account else None,
                "account_number": selected_account.account_number if selected_account else None,
                "selected_account_id": str(selected_account.id) if selected_account else None,
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/credits", status_code=303)
