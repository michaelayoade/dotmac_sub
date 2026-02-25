"""Admin billing reporting/config routes."""

from decimal import Decimal, InvalidOperation
from typing import Any, cast

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.billing import TaxRateCreate
from app.services import billing as billing_service
from app.services import web_billing_ledger as web_billing_ledger_service
from app.services import web_billing_overview as web_billing_overview_service
from app.services import web_billing_tax_rates as web_billing_tax_rates_service
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _parse_decimal(value: str | None, field: str, default: Decimal | None = None) -> Decimal:
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a valid number") from exc


@router.get("/tax-rates", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_tax_rates(request: Request, db: Session = Depends(get_db)):
    state = web_billing_tax_rates_service.list_data(db)
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/tax_rates.html",
        {
            "request": request,
            **state,
            "active_page": "tax-rates",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/tax-rates", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def billing_tax_rate_create(
    request: Request,
    name: str = Form(...),
    rate: str = Form(...),
    code: str | None = Form(None),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = TaxRateCreate(
            name=name.strip(),
            rate=_parse_decimal(rate, "rate"),
            code=code.strip() if code else None,
            description=description.strip() if description else None,
        )
        billing_service.tax_rates.create(db, payload)
    except Exception as exc:
        state = web_billing_tax_rates_service.list_data(db)
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/tax_rates.html",
            {
                "request": request,
                **state,
                "error": str(exc),
                "active_page": "tax-rates",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/tax-rates", status_code=303)


@router.get("/ar-aging", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_ar_aging(
    request: Request,
    period: str = Query("all"),
    bucket: str | None = Query(None),
    partner_id: str | None = Query(None),
    location: str | None = Query(None),
    debtor_period: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_overview_service.build_ar_aging_data(
        db,
        period=period,
        bucket=bucket,
        partner_id=partner_id,
        location=location,
        debtor_period=debtor_period,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/ar_aging.html",
        {
            "request": request,
            **state,
            "active_page": "ar-aging",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/ledger", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_ledger(
    request: Request,
    customer_ref: str | None = Query(None),
    date_range: str | None = Query(None),
    category: str | None = Query(None),
    partner_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    entry_type = request.query_params.get("entry_type")
    state = web_billing_ledger_service.build_ledger_entries_data(
        db,
        customer_ref=customer_ref,
        entry_type=entry_type,
        date_range=date_range,
        category=category,
        partner_id=partner_id,
    )
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/ledger.html",
        {
            "request": request,
            **state,
            "active_page": "ledger",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/ledger/export.csv", dependencies=[Depends(require_permission("billing:read"))])
def billing_ledger_export_csv(
    request: Request,
    customer_ref: str | None = Query(None),
    entry_type: str | None = Query(None),
    date_range: str | None = Query(None),
    category: str | None = Query(None),
    partner_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_ledger_service.build_ledger_entries_data(
        db,
        customer_ref=customer_ref,
        entry_type=entry_type,
        date_range=date_range,
        category=category,
        partner_id=partner_id,
        limit=10000,
    )
    content = web_billing_ledger_service.render_ledger_csv(cast(list[Any], state["entries"]))
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ledger_export.csv"'},
    )
