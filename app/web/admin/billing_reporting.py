"""Admin billing reporting/config routes."""

from typing import Any, cast

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import TaxRate
from app.services import billing as billing_service
from app.services import web_billing_ledger as web_billing_ledger_service
from app.services import web_billing_overview as web_billing_overview_service
from app.services import web_billing_tax_rates as web_billing_tax_rates_service
from app.services.audit_helpers import (
    build_audit_activities_for_types,
    log_audit_event,
)
from app.services.auth_dependencies import require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _actor_id(request: Request) -> str | None:
    return getattr(request.state, "actor_id", None)


def _tax_rate_snapshot(rate: TaxRate | None) -> dict[str, object | None]:
    if rate is None:
        return {}
    return {
        "name": rate.name,
        "code": rate.code,
        "rate": str(rate.rate),
        "description": rate.description,
        "is_active": rate.is_active,
    }


def _created_changes(snapshot: dict[str, object | None]) -> dict[str, dict[str, object | None]]:
    return {key: {"from": None, "to": value} for key, value in snapshot.items()}


def _tax_rate_audit_items(db: Session, limit: int = 5) -> list[dict]:
    try:
        return build_audit_activities_for_types(db, ["tax_rate"], limit=limit)
    except Exception:
        db.rollback()
        return []


@router.get(
    "/tax-rates",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:tax:read"))],
)
def billing_tax_rates(request: Request, db: Session = Depends(get_db)):
    state = web_billing_tax_rates_service.list_data(db)
    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/tax_rates.html",
        {
            "request": request,
            **state,
            "audit_items": _tax_rate_audit_items(db),
            "active_page": "tax-rates",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/tax-rates",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:tax:write"))],
)
def billing_tax_rate_create(
    request: Request,
    name: str = Form(...),
    rate: str = Form(...),
    code: str | None = Form(None),
    description: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        tax_rate = web_billing_tax_rates_service.create_tax_rate_from_form(
            db,
            name=name,
            rate=rate,
            code=code,
            description=description,
        )
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="tax_rate",
            entity_id=str(tax_rate.id),
            actor_id=_actor_id(request),
            metadata={"changes": _created_changes(_tax_rate_snapshot(tax_rate))},
        )
    except Exception as exc:
        db.rollback()
        state = web_billing_tax_rates_service.list_data(db)
        from app.web.admin import get_current_user, get_sidebar_stats

        return templates.TemplateResponse(
            "admin/billing/tax_rates.html",
            {
                "request": request,
                **state,
                "audit_items": _tax_rate_audit_items(db),
                "error": str(exc),
                "active_page": "tax-rates",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/tax-rates", status_code=303)


@router.post(
    "/tax-rates/{rate_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:tax:write"))],
)
def billing_tax_rate_toggle(
    request: Request,
    rate_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    tax_rate = billing_service.tax_rates.get(db, rate_id)
    before = _tax_rate_snapshot(tax_rate)
    tax_rate = web_billing_tax_rates_service.toggle_tax_rate(db, rate_id=rate_id)
    after = _tax_rate_snapshot(tax_rate)
    if before != after:
        log_audit_event(
            db=db,
            request=request,
            action="toggle_active",
            entity_type="tax_rate",
            entity_id=str(tax_rate.id),
            actor_id=_actor_id(request),
            metadata={
                "changes": {
                    key: {"from": before.get(key), "to": after.get(key)}
                    for key in sorted(set(before) | set(after))
                    if before.get(key) != after.get(key)
                }
            },
        )
    return RedirectResponse(url="/admin/billing/tax-rates", status_code=303)


@router.get(
    "/ar-aging",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
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


@router.get(
    "/ledger",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
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


@router.get(
    "/ledger/export.csv",
    dependencies=[Depends(require_permission("billing:ledger:read"))],
)
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
    content = web_billing_ledger_service.render_ledger_csv(
        cast(list[Any], state["entries"])
    )
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ledger_export.csv"'},
    )
