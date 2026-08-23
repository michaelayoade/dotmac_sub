"""Admin billing credit note routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_credits as web_billing_credits_service
from app.services import (
    web_billing_tax_reconciliation as web_billing_tax_reconciliation_service,
)
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
    "/tax-reconciliation",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:tax:read"))],
)
def billing_tax_reconciliation(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/tax_reconciliation.html",
        {
            "request": request,
            **web_billing_tax_reconciliation_service.build_tax_reconciliation_data(
                db,
                page=page,
                per_page=per_page,
            ),
            "active_page": "tax-reconciliation",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/tax-reconciliation/{invoice_id:uuid}/credit/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:credit_note:create"))],
)
def billing_tax_reconciliation_credit_preview(
    request: Request,
    invoice_id: UUID,
    candidate_fingerprint: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    try:
        review = web_billing_tax_reconciliation_service.prepare_tax_credit_review(
            db,
            invoice_id=invoice_id,
            candidate_fingerprint=candidate_fingerprint,
        )
    except web_billing_tax_reconciliation_service.TaxReconciliationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return templates.TemplateResponse(
        "admin/billing/tax_reconciliation_credit_confirm.html",
        {
            "request": request,
            "review": review,
            "active_page": "tax-reconciliation",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/tax-reconciliation/{invoice_id:uuid}/credit",
    dependencies=[Depends(require_permission("billing:credit_note:create"))],
)
def billing_tax_reconciliation_credit_create(
    invoice_id: UUID,
    candidate_fingerprint: str = Form(...),
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        result = web_billing_tax_reconciliation_service.issue_tax_credit(
            db,
            invoice_id=invoice_id,
            candidate_fingerprint=candidate_fingerprint,
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
        )
    except web_billing_tax_reconciliation_service.TaxReconciliationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    return RedirectResponse(
        url=(
            f"/admin/billing/invoices/{invoice_id}"
            f"?notice=Tax+credit+{result.credit_note.credit_number}+issued"
        ),
        status_code=303,
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
    "/credits/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:credit_note:create"))],
)
def billing_credit_preview(
    request: Request,
    account_id: str = Form(...),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        state = web_billing_credits_service.preview_credit_from_form(
            db,
            account_id=account_id,
            amount=amount,
            currency=currency,
            memo=memo,
        )
    except Exception as exc:
        db.rollback()
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
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/credit_issue_confirm.html",
        {
            "request": request,
            **state,
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
    preview_fingerprint: str = Form(...),
    idempotency_key: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        web_billing_credits_service.issue_credit_from_form(
            db,
            request=request,
            actor_id=None,
            account_id=account_id,
            amount=amount,
            currency=currency,
            memo=memo,
            preview_fingerprint=preview_fingerprint,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        db.rollback()
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
            status_code=409,
        )
    return RedirectResponse(url="/admin/billing/credits", status_code=303)
