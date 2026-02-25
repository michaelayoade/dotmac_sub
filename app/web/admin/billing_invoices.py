"""Admin billing management web routes."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_invoice_forms as web_billing_invoice_forms_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services import web_billing_overview as web_billing_overview_service
from app.services.audit_helpers import (
    log_audit_event,
)
from app.services.auth_dependencies import require_permission
from app.services.billing import configuration as billing_config_service

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _placeholder_context(request: Request, db: Session, title: str, active_page: str):
    from app.web.admin import get_current_user, get_sidebar_stats
    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "billing",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "page_title": title,
        "heading": title,
        "description": f"{title} management will appear here.",
        "empty_title": f"No {title.lower()} yet",
        "empty_message": "Billing configuration will appear once it is enabled.",
    }


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


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_overview(
    request: Request,
    partner_id: str | None = Query(None),
    location: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Billing overview page."""
    state = web_billing_overview_service.build_overview_data(
        db,
        partner_id=partner_id,
        location=location,
    )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/index.html",
        {
            "request": request,
            **state,
            "active_page": "billing",
            "active_menu": "billing",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/invoices", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoices_list(
    request: Request,
    account_id: str | None = None,
    partner_id: str | None = Query(None),
    status: str | None = None,
    proforma_only: bool = Query(False),
    customer_ref: str | None = Query(None),
    search: str | None = Query(None),
    date_range: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all invoices with filtering."""
    state = web_billing_overview_service.build_invoices_list_data(
        db,
        account_id=account_id,
        partner_id=partner_id,
        status=status,
        proforma_only=proforma_only,
        customer_ref=customer_ref,
        search=search,
        date_range=date_range,
        page=page,
        per_page=per_page,
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "admin/billing/_invoices_table.html",
            {
                "request": request,
                "invoices": state["invoices"],
                "page": state["page"],
                "per_page": state["per_page"],
                "total": state["total"],
                "total_pages": state["total_pages"],
                "status_totals": state["status_totals"],
                "status": state["status"],
                "proforma_only": state["proforma_only"],
                "customer_ref": state["customer_ref"],
                "selected_partner_id": state["selected_partner_id"],
                "search": state["search"],
                "date_range": state["date_range"],
            },
        )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/invoices.html",
        {
            "request": request,
            **state,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/invoices/export.csv", dependencies=[Depends(require_permission("billing:read"))])
def invoices_export_csv(
    request: Request,
    account_id: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    proforma_only: bool = Query(False),
    customer_ref: str | None = Query(None),
    search: str | None = Query(None),
    date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_overview_service.build_invoices_list_data(
        db,
        account_id=account_id,
        partner_id=partner_id,
        status=status,
        proforma_only=proforma_only,
        customer_ref=customer_ref,
        search=search,
        date_range=date_range,
        page=1,
        per_page=10000,
    )
    content = web_billing_overview_service.render_invoices_csv(cast(list[Any], state["invoices"]))
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="invoices_export.csv"'},
    )


@router.get("/invoices/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_new(
    request: Request,
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_invoice_forms_service.new_form_state(
        db,
        account_id=account_id or account,
    )

    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/invoice_form.html",
        {
            "request": request,
            **state,
            "invoice": None,
            "action_url": "/admin/billing/invoices/create",
            "form_title": "New Invoice",
            "submit_label": "Create Invoice",
            "show_line_items": True,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/invoices/create", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_create(
    request: Request,
    account_id: str | None = Form(None),
    customer_ref: str | None = Form(None),
    invoice_number: str | None = Form(None),
    status: str | None = Form(None),
    currency: str = Form("NGN"),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    memo: str | None = Form(None),
    proforma_invoice: str | None = Form(None),
    line_description: list[str] = Form([]),
    line_quantity: list[str] = Form([]),
    line_unit_price: list[str] = Form([]),
    line_tax_rate_id: list[str] = Form([]),
    line_items_json: str | None = Form(None),
    issue_immediately: str | None = Form(None),
    send_notification: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        invoice, resolved_account_id = web_billing_invoices_service.create_invoice_from_form(
            db,
            account_id=account_id,
            customer_ref=customer_ref,
            invoice_number=invoice_number,
            status=status,
            currency=currency,
            issued_at=issued_at,
            due_at=due_at,
            memo=memo,
            proforma_invoice=proforma_invoice,
            line_description=line_description,
            line_quantity=line_quantity,
            line_unit_price=line_unit_price,
            line_tax_rate_id=line_tax_rate_id,
            line_items_json=line_items_json,
            issue_immediately=issue_immediately,
            send_notification=send_notification,
            parse_uuid=_parse_uuid,
            parse_datetime=_parse_datetime,
            parse_decimal=_parse_decimal,
        )
    except Exception as exc:
        state = web_billing_invoice_forms_service.new_form_state(
            db,
            account_id=locals().get("resolved_account_id") or account_id,
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/invoice_form.html",
            {
                "request": request,
                **state,
                "action_url": "/admin/billing/invoices/create",
                "form_title": "New Invoice",
                "submit_label": "Create Invoice",
                "error": str(exc),
                "active_page": "invoices",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="invoice",
        entity_id=str(invoice.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"invoice_number": invoice.invoice_number},
    )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice.id}", status_code=303)


@router.get("/invoices/{invoice_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_edit(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_invoice_forms_service.edit_form_state(
        db,
        invoice_id=str(invoice_id),
    )
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/invoice_form.html",
        {
            "request": request,
            **state,
            "action_url": f"/admin/billing/invoices/{invoice_id}/edit",
            "form_title": "Edit Invoice",
            "submit_label": "Save Changes",
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/invoices/{invoice_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_update(
    request: Request,
    invoice_id: UUID,
    account_id: str | None = Form(None),
    invoice_number: str | None = Form(None),
    status: str | None = Form(None),
    currency: str = Form("NGN"),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    memo: str | None = Form(None),
    proforma_invoice: str | None = Form(None),
    line_items_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        _, metadata_payload = web_billing_invoices_service.update_invoice_from_form(
            db,
            invoice_id=str(invoice_id),
            account_id=account_id,
            invoice_number=invoice_number,
            status=status,
            currency=currency,
            issued_at=issued_at,
            due_at=due_at,
            memo=memo,
            proforma_invoice=proforma_invoice,
            line_items_json=line_items_json,
            parse_uuid=_parse_uuid,
            parse_datetime=_parse_datetime,
        )
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="invoice",
            entity_id=str(invoice_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except Exception as exc:
        state = web_billing_invoice_forms_service.edit_form_state(
            db,
            invoice_id=str(invoice_id),
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/invoice_form.html",
            {
                "request": request,
                **(state or {}),
                "action_url": f"/admin/billing/invoices/{invoice_id}/edit",
                "form_title": "Edit Invoice",
                "submit_label": "Save Changes",
                "show_line_items": False,
                "error": str(exc),
                "active_page": "invoices",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice_id}", status_code=303)


@router.get("/invoices/search", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_search(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse("")


@router.get("/invoices/filter", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_filter(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse("")

