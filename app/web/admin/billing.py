"""Admin billing management web routes."""

from datetime import UTC, datetime
import io
from decimal import Decimal, InvalidOperation
from time import sleep
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID
import zipfile

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import CreditNoteStatus, Payment
from app.models.catalog import BillingCycle
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    CollectionAccountUpdate,
    CreditNoteApplyRequest,
    CreditNoteCreate,
    InvoiceCreate,
    InvoiceLineCreate,
    InvoiceUpdate,
    PaymentProviderCreate,
    PaymentProviderUpdate,
    TaxRateCreate,
)
from app.services import billing as billing_service
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services import notification as notification_service
from app.services import payment_arrangements as payment_arrangements_service
from app.services import subscriber as subscriber_service
from app.services import web_billing_accounts as web_billing_accounts_service
from app.services import web_billing_arrangements as web_billing_arrangements_service
from app.services import web_billing_channels as web_billing_channels_service
from app.services import (
    web_billing_collection_accounts as web_billing_collection_accounts_service,
)
from app.services import web_billing_credits as web_billing_credits_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services import web_billing_dunning as web_billing_dunning_service
from app.services import (
    web_billing_invoice_actions as web_billing_invoice_actions_service,
)
from app.services import web_billing_invoice_batch as web_billing_invoice_batch_service
from app.services import web_billing_invoice_bulk as web_billing_invoice_bulk_service
from app.services import web_billing_invoice_forms as web_billing_invoice_forms_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services import web_billing_ledger as web_billing_ledger_service
from app.services import web_billing_overview as web_billing_overview_service
from app.services import web_billing_payment_forms as web_billing_payment_forms_service
from app.services import web_billing_payments as web_billing_payments_service
from app.services import web_billing_providers as web_billing_providers_service
from app.services import web_billing_reconciliation as web_billing_reconciliation_service
from app.services import web_billing_statements as web_billing_statements_service
from app.services import web_billing_tax_rates as web_billing_tax_rates_service
from app.services.audit_helpers import (
    build_audit_activities,
    build_changes_metadata,
    log_audit_event,
)
from app.services.auth_dependencies import require_permission
from app.services.billing import configuration as billing_config_service
from app.services.file_storage import build_content_disposition
from app.services.object_storage import ObjectNotFoundError
from app.web.request_parsing import parse_json_body
from app.models.notification import NotificationChannel, NotificationStatus
from app.schemas.notification import NotificationCreate

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _invoice_pdf_response(
    db: Session,
    latest_export,
    invoice,
):
    try:
        stream = billing_invoice_pdf_service.stream_export(db, latest_export)
    except ObjectNotFoundError:
        return None

    headers = {
        "Content-Disposition": build_content_disposition(
            billing_invoice_pdf_service.download_filename(invoice)
        )
    }
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(
        stream.chunks,
        media_type=stream.content_type or "application/pdf",
        headers=headers,
    )


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


def _parse_billing_cycle(value: str | None) -> BillingCycle | None:
    if not value:
        return None
    try:
        return BillingCycle(value)
    except ValueError as exc:
        raise ValueError("Invalid billing cycle") from exc


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
        resolved_account_id = account_id
        if not resolved_account_id and customer_ref:
            customer_accounts = web_billing_customers_service.accounts_for_customer(db, customer_ref)
            if len(customer_accounts) == 1:
                resolved_account_id = str(customer_accounts[0]["id"])
            elif len(customer_accounts) > 1:
                raise ValueError("Please select a billing account for the selected customer.")
            else:
                raise ValueError("No billing account found for the selected customer.")
        invoice_number, memo = web_billing_invoices_service.apply_proforma_form_values(
            invoice_number=invoice_number,
            memo=memo,
            proforma_invoice=bool(proforma_invoice),
        )

        payload_data = web_billing_invoices_service.build_invoice_payload_data(
            account_id=_parse_uuid(resolved_account_id, "account_id"),
            invoice_number=invoice_number,
            status=status,
            currency=currency,
            issued_at=_parse_datetime(issued_at),
            due_at=_parse_datetime(due_at),
            memo=memo,
            is_proforma=bool(proforma_invoice),
        )
        payload = InvoiceCreate.model_validate(payload_data)
        invoice = billing_service.invoices.create(db=db, payload=payload)
        line_items = web_billing_invoices_service.parse_create_line_items(
            line_items_json=line_items_json,
            line_description=line_description,
            line_quantity=line_quantity,
            line_unit_price=line_unit_price,
            line_tax_rate_id=line_tax_rate_id,
            parse_decimal=_parse_decimal,
        )
        web_billing_invoices_service.create_invoice_lines(
            db,
            invoice_id=invoice.id,
            line_items=line_items,
        )
        issued_invoice = web_billing_invoices_service.maybe_issue_invoice(
            db,
            invoice_id=invoice.id,
            issue_immediately=issue_immediately,
        )
        if issued_invoice:
            invoice = issued_invoice
        web_billing_invoices_service.maybe_send_invoice_notification(
            db,
            invoice=invoice,
            send_notification=send_notification,
        )
    except Exception as exc:
        state = web_billing_invoice_forms_service.new_form_state(
            db,
            account_id=resolved_account_id if "resolved_account_id" in locals() else account_id,
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
        before = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
        invoice_number, memo = web_billing_invoices_service.apply_proforma_form_values(
            invoice_number=invoice_number,
            memo=memo,
            proforma_invoice=bool(proforma_invoice),
        )
        payload_data = web_billing_invoices_service.build_invoice_payload_data(
            account_id=_parse_uuid(account_id, "account_id"),
            invoice_number=invoice_number,
            status=status,
            currency=currency,
            issued_at=_parse_datetime(issued_at),
            due_at=_parse_datetime(due_at),
            memo=memo,
            is_proforma=bool(proforma_invoice),
        )
        payload = InvoiceUpdate.model_validate(payload_data)
        billing_service.invoices.update(db=db, invoice_id=str(invoice_id), payload=payload)
        web_billing_invoices_service.apply_line_items_json_update(
            db,
            invoice_id=invoice_id,
            before_invoice=before,
            line_items_json=line_items_json,
        )
        after = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
        metadata_payload = build_changes_metadata(before, after)
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


@router.post(
    "/invoices/{invoice_id}/convert-proforma",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def invoice_convert_proforma(
    request: Request,
    invoice_id: UUID,
    db: Session = Depends(get_db),
):
    converted = web_billing_invoices_service.convert_proforma_to_final(
        db,
        invoice_id=str(invoice_id),
    )
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="convert",
        entity_type="invoice",
        entity_id=str(invoice_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={"from": "proforma", "to": "final", "invoice_number": converted.invoice_number},
    )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice_id}", status_code=303)


@router.get("/invoices/search", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_search(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse("")


@router.get("/invoices/filter", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_filter(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse("")


@router.post("/invoices/generate-batch", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_generate_batch(
    request: Request,
    billing_cycle: str | None = Form(None),
    billing_date: str | None = Form(None),
    db: Session = Depends(get_db),
):
    note = web_billing_invoice_batch_service.run_batch_with_date(
        db,
        billing_cycle=billing_cycle,
        billing_date=billing_date,
        parse_cycle_fn=_parse_billing_cycle,
    )
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"{note}"
        "</div>"
    )


@router.post("/invoices/batch/{run_id}/retry", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_batch_retry(
    request: Request,
    run_id: str,
    db: Session = Depends(get_db),
):
    note = web_billing_invoice_batch_service.retry_batch_run(
        db,
        run_id=run_id,
        parse_cycle_fn=_parse_billing_cycle,
    )
    query = urlencode({"note": note})
    return RedirectResponse(url=f"/admin/billing/invoices/batch?{query}", status_code=303)


@router.get("/invoices/batch", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_batch(
    request: Request,
    note: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats
    today = web_billing_invoice_actions_service.batch_today_str()
    recent_runs = web_billing_invoice_batch_service.list_recent_runs(db, limit=25)
    schedule_config = web_billing_invoice_batch_service.get_billing_run_schedule(db)
    partner_options = [
        {"id": str(item.id), "name": item.name}
        for item in db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .order_by(Reseller.name.asc())
        .all()
    ]
    return templates.TemplateResponse(
        "admin/billing/invoice_batch.html",
        {
            "request": request,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "today": today,
            "recent_runs": recent_runs,
            "note": note,
            "schedule_config": schedule_config,
            "schedule_partner_options": partner_options,
        },
    )


@router.post("/invoices/batch/schedule", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_batch_schedule_update(
    request: Request,
    schedule_enabled: str | None = Form(None),
    run_day: str | None = Form(None),
    run_time: str | None = Form(None),
    timezone: str | None = Form(None),
    billing_cycle: str | None = Form(None),
    partner_ids: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    web_billing_invoice_batch_service.save_billing_run_schedule(
        db,
        enabled=bool(schedule_enabled),
        run_day=run_day,
        run_time=run_time,
        timezone=timezone,
        billing_cycle=billing_cycle,
        partner_ids=partner_ids,
    )
    return RedirectResponse(
        url="/admin/billing/invoices/batch?note=Billing+run+schedule+saved",
        status_code=303,
    )


@router.get("/invoices/batch/history-panel", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_batch_history_panel(
    request: Request,
    db: Session = Depends(get_db),
):
    recent_runs = web_billing_invoice_batch_service.list_recent_runs(db, limit=25)
    return templates.TemplateResponse(
        "admin/billing/_invoice_batch_history_table.html",
        {
            "request": request,
            "recent_runs": recent_runs,
        },
    )


@router.get("/invoices/batch/history.csv", dependencies=[Depends(require_permission("billing:read"))])
def invoice_batch_history_csv(
    request: Request,
    db: Session = Depends(get_db),
):
    rows = web_billing_invoice_batch_service.list_recent_runs(db, limit=1000)
    content = web_billing_invoice_batch_service.render_runs_csv(rows)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="billing_run_history.csv"'},
    )


@router.get("/invoices/batch/{run_id}/export.csv", dependencies=[Depends(require_permission("billing:read"))])
def invoice_batch_run_csv(
    request: Request,
    run_id: str,
    db: Session = Depends(get_db),
):
    row = web_billing_invoice_batch_service.get_run_row(db, run_id=run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Billing run not found")
    content = web_billing_invoice_batch_service.render_single_run_csv(row)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="billing_run_{run_id}.csv"'},
    )


@router.post("/invoices/generate-batch/preview", dependencies=[Depends(require_permission("billing:read"))])
def invoice_generate_batch_preview(
    request: Request,
    billing_cycle: str | None = Form(None),
    subscription_status: str | None = Form(None),
    billing_date: str | None = Form(None),
    invoice_status: str | None = Form(None),
    separate_by_partner: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Dry-run preview of batch invoice generation."""
    from fastapi.responses import JSONResponse

    try:
        payload = web_billing_invoice_batch_service.preview_batch(
            db=db,
            billing_cycle=billing_cycle,
            billing_date=billing_date,
            separate_by_partner=bool(separate_by_partner),
            parse_cycle_fn=_parse_billing_cycle,
        )
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse(web_billing_invoice_batch_service.preview_error_payload(exc), status_code=400)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_detail(
    request: Request,
    invoice_id: UUID,
    pdf_notice: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """View invoice details."""
    detail_data = web_billing_invoices_service.load_invoice_detail_data(
        db,
        invoice_id=str(invoice_id),
    )
    if not detail_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)
    return templates.TemplateResponse(
        "admin/billing/invoice_detail.html",
        {
            "request": request,
            **detail_data,
            "pdf_notice": pdf_notice,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.post("/invoices/{invoice_id}/lines", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_line_create(
    request: Request,
    invoice_id: UUID,
    description: str = Form(...),
    quantity: str = Form("1"),
    unit_price: str = Form("0"),
    tax_rate_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        payload = InvoiceLineCreate(
            invoice_id=_parse_uuid(str(invoice_id), "invoice_id"),
            description=description.strip(),
            quantity=_parse_decimal(quantity, "quantity"),
            unit_price=_parse_decimal(unit_price, "unit_price"),
            tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
        )
        billing_service.invoice_lines.create(db, payload)
    except Exception as exc:
        detail_data = web_billing_invoices_service.load_invoice_detail_data(
            db,
            invoice_id=str(invoice_id),
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/invoice_detail.html",
            {
                "request": request,
                **(detail_data or {}),
                "error": str(exc),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id}/apply-credit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_apply_credit(
    request: Request,
    invoice_id: UUID,
    credit_note_id: str = Form(...),
    amount: str | None = Form(None),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        before = billing_service.credit_notes.get(db=db, credit_note_id=credit_note_id)
        payload = CreditNoteApplyRequest(
            invoice_id=_parse_uuid(str(invoice_id), "invoice_id"),
            amount=_parse_decimal(amount, "amount") if amount else None,
            memo=memo.strip() if memo else None,
        )
        billing_service.credit_notes.apply(db, credit_note_id, payload)
        after = billing_service.credit_notes.get(db=db, credit_note_id=credit_note_id)
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="apply",
            entity_type="credit_note",
            entity_id=str(credit_note_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except Exception as exc:
        detail_data = web_billing_invoices_service.load_invoice_detail_data(
            db,
            invoice_id=str(invoice_id),
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/invoice_detail.html",
            {
                "request": request,
                **(detail_data or {}),
                "error": str(exc),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/billing/invoices/{invoice_id}", status_code=303)


@router.get("/invoices/{invoice_id}/pdf", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_pdf(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
    if not invoice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    db.expire_all()
    latest_export = billing_invoice_pdf_service.get_latest_export(
        db,
        invoice_id=str(invoice_id),
    )
    latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(db, latest_export)
    if billing_invoice_pdf_service.export_file_exists(db, latest_export):
        response = _invoice_pdf_response(db, latest_export, invoice)
        if response is not None:
            return response

    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    actor_id = current_user.get("subscriber_id")
    export = billing_invoice_pdf_service.queue_export(
        db,
        invoice_id=str(invoice_id),
        requested_by_id=str(actor_id) if actor_id else None,
    )

    # Prefer an immediate download experience when possible:
    # poll briefly for worker completion, then run inline as fallback.
    for _ in range(5):
        db.expire_all()
        latest_export = billing_invoice_pdf_service.get_latest_export(
            db,
            invoice_id=str(invoice_id),
        )
        latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(db, latest_export)
        if billing_invoice_pdf_service.export_file_exists(db, latest_export):
            response = _invoice_pdf_response(db, latest_export, invoice)
            if response is not None:
                return response
        sleep(0.4)

    try:
        billing_invoice_pdf_service.process_export(str(export.id))
    except Exception:
        pass

    db.expire_all()
    latest_export = billing_invoice_pdf_service.get_latest_export(
        db,
        invoice_id=str(invoice_id),
    )
    latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(db, latest_export)
    if billing_invoice_pdf_service.export_file_exists(db, latest_export):
        response = _invoice_pdf_response(db, latest_export, invoice)
        if response is not None:
            return response

    notice = "queued"
    status_value = export.status.value
    if latest_export and latest_export.status:
        status_value = latest_export.status.value
    if status_value == "processing":
        notice = "processing"
    elif status_value == "failed":
        notice = "failed"

    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}?pdf_notice={notice}",
        status_code=303,
    )


@router.get(
    "/invoices/{invoice_id}/pdf/download",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def invoice_pdf_download(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
    if not invoice:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Invoice not found"},
            status_code=404,
        )

    db.expire_all()
    latest_export = billing_invoice_pdf_service.get_latest_export(
        db,
        invoice_id=str(invoice_id),
    )
    latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(db, latest_export)
    if billing_invoice_pdf_service.export_file_exists(db, latest_export):
        response = _invoice_pdf_response(db, latest_export, invoice)
        if response is not None:
            return response

    return RedirectResponse(
        url=f"/admin/billing/invoices/{invoice_id}?pdf_notice=not_ready",
        status_code=303,
    )


@router.post("/invoices/{invoice_id}/send", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_send(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
    if invoice:
        web_billing_invoices_service.maybe_send_invoice_notification(
            db,
            invoice=invoice,
            send_notification="1",
        )
    log_audit_event(
        db=db,
        request=request,
        action="send",
        entity_type="invoice",
        entity_id=str(invoice_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return HTMLResponse(web_billing_invoice_actions_service.send_message(invoice_id))


@router.post("/invoices/{invoice_id}/send-and-return", dependencies=[Depends(require_permission("billing:write"))])
def invoice_send_and_return(
    request: Request,
    invoice_id: UUID,
    next_url: str = Form("/admin/billing/invoices"),
    db: Session = Depends(get_db),
):
    invoice_send(request=request, invoice_id=invoice_id, db=db)
    return RedirectResponse(url=next_url, status_code=303)


@router.post("/invoices/{invoice_id}/void", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_void(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="void",
        entity_type="invoice",
        entity_id=str(invoice_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return HTMLResponse(web_billing_invoice_actions_service.void_message(invoice_id))


@router.post("/invoices/bulk/issue", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_issue(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk issue invoices (change status from draft to issued)."""
    from fastapi.responses import JSONResponse

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    updated_ids = web_billing_invoice_bulk_service.bulk_issue(db, invoice_ids)
    count = len(updated_ids)
    for invoice_id in updated_ids:
        log_audit_event(
            db=db,
            request=request,
            action="issue",
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )

    return JSONResponse({"message": f"Issued {count} invoices", "count": count})


@router.post("/invoices/bulk/send", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_send(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk send invoice notifications."""
    from fastapi.responses import JSONResponse

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    queued_ids = web_billing_invoice_bulk_service.bulk_send(db, invoice_ids)
    count = len(queued_ids)
    for invoice_id in queued_ids:
        log_audit_event(
            db=db,
            request=request,
            action="send",
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )

    return JSONResponse({"message": f"Queued {count} invoice notifications", "count": count})


@router.post("/invoices/bulk/void", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_void(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk void invoices."""
    from fastapi.responses import JSONResponse

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    updated_ids = web_billing_invoice_bulk_service.bulk_void(db, invoice_ids)
    count = len(updated_ids)
    for invoice_id in updated_ids:
        log_audit_event(
            db=db,
            request=request,
            action="void",
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )

    return JSONResponse({"message": f"Voided {count} invoices", "count": count})


@router.post("/invoices/bulk/mark-paid", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_mark_paid(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Bulk mark invoices as paid."""
    from fastapi.responses import JSONResponse

    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    updated_ids = web_billing_invoice_bulk_service.bulk_mark_paid(db, invoice_ids)
    count = len(updated_ids)
    for invoice_id in updated_ids:
        log_audit_event(
            db=db,
            request=request,
            action="mark_paid",
            entity_type="invoice",
            entity_id=invoice_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )

    return JSONResponse({"message": f"Marked {count} invoices as paid", "count": count})


@router.post("/invoices/bulk/generate-pdf", dependencies=[Depends(require_permission("billing:read"))])
def invoice_bulk_generate_pdf(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    """Queue invoice PDF generation for selected invoices."""
    from fastapi.responses import JSONResponse

    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    actor_id = current_user.get("subscriber_id")
    result = web_billing_invoice_bulk_service.bulk_queue_pdf_exports(
        db,
        invoice_ids,
        requested_by_id=str(actor_id) if actor_id else None,
    )
    queued = len(result["queued"])
    ready = len(result["ready"])
    missing = len(result["missing"])
    return JSONResponse(
        {
            "message": f"Queued {queued} PDF export(s), {ready} already ready, {missing} skipped",
            "count": queued,
            "queued": queued,
            "ready": ready,
            "skipped": missing,
        }
    )


@router.get("/invoices/bulk/pdf-ready", dependencies=[Depends(require_permission("billing:read"))])
def invoice_bulk_pdf_ready(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    from fastapi.responses import JSONResponse

    payload = web_billing_invoice_bulk_service.bulk_pdf_readiness(db, invoice_ids)
    return JSONResponse(payload)


@router.get("/invoices/bulk/export.csv", dependencies=[Depends(require_permission("billing:read"))])
def invoice_bulk_export_csv(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    invoices = web_billing_invoice_bulk_service.list_invoices_by_ids(db, invoice_ids)
    content = web_billing_overview_service.render_invoices_csv(cast(list[Any], invoices))
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="invoices_selected_export.csv"'},
    )


@router.get("/invoices/bulk/export.zip", dependencies=[Depends(require_permission("billing:read"))])
def invoice_bulk_export_pdf_zip(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    invoices = web_billing_invoice_bulk_service.list_invoices_by_ids(db, invoice_ids)
    archive_buffer = io.BytesIO()
    skipped: list[str] = []
    used_names: set[str] = set()

    with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for invoice in invoices:
            latest_export = billing_invoice_pdf_service.get_latest_export(db, invoice_id=str(invoice.id))
            latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(db, latest_export)
            if not billing_invoice_pdf_service.export_file_exists(db, latest_export):
                skipped.append(str(invoice.invoice_number or invoice.id))
                continue
            try:
                stream = billing_invoice_pdf_service.stream_export(db, latest_export)
                pdf_bytes = b"".join(stream.chunks)
            except ObjectNotFoundError:
                skipped.append(str(invoice.invoice_number or invoice.id))
                continue

            filename = billing_invoice_pdf_service.download_filename(invoice)
            if filename in used_names:
                stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
                suffix = 2
                while f"{stem}_{suffix}.pdf" in used_names:
                    suffix += 1
                filename = f"{stem}_{suffix}.pdf"
            used_names.add(filename)
            archive.writestr(filename, pdf_bytes)

        if skipped:
            archive.writestr(
                "README.txt",
                "Some selected invoices were skipped because PDF exports were not ready:\n"
                + "\n".join(f"- {value}" for value in skipped),
            )
        elif not invoices:
            archive.writestr("README.txt", "No invoices were selected.")

    content = archive_buffer.getvalue()
    return StreamingResponse(
        iter([content]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="invoices_selected_pdfs.zip"'},
    )


@router.get("/credits", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_credits_list(
    request: Request,
    page: int = 1,
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """List all credit notes."""
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


@router.get("/payments", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payments_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    date_range: str | None = Query(None),
    unallocated_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    """List all payments."""
    state = web_billing_payments_service.build_payments_list_data(
        db,
        page=page,
        per_page=per_page,
        customer_ref=customer_ref,
        partner_id=partner_id,
        status=status,
        method=method,
        search=search,
        date_range=date_range,
        unallocated_only=unallocated_only,
    )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/payments.html",
        {
            "request": request,
            **state,
            "page_heading": "Unallocated Payments" if unallocated_only else "Payments",
            "page_subtitle": "Payments with no invoice allocations" if unallocated_only else "Track all payment transactions and collections",
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/payments/export.csv", dependencies=[Depends(require_permission("billing:read"))])
def payments_export_csv(
    request: Request,
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    date_range: str | None = Query(None),
    unallocated_only: bool = Query(False),
    db: Session = Depends(get_db),
):
    state = web_billing_payments_service.build_payments_list_data(
        db,
        page=1,
        per_page=10000,
        customer_ref=customer_ref,
        partner_id=partner_id,
        status=status,
        method=method,
        search=search,
        date_range=date_range,
        unallocated_only=unallocated_only,
    )
    content = web_billing_payments_service.render_payments_csv(cast(list[Payment], state["payments"]))
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename=\"payments_export.csv\"'},
    )


@router.get("/payments/unallocated", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payments_unallocated(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    partner_id: str | None = Query(None),
    status: str | None = Query(None),
    method: str | None = Query(None),
    search: str | None = Query(None),
    date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return payments_list(
        request=request,
        page=page,
        per_page=per_page,
        customer_ref=customer_ref,
        partner_id=partner_id,
        status=status,
        method=method,
        search=search,
        date_range=date_range,
        unallocated_only=True,
        db=db,
    )


@router.get("/payments/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_new(
    request: Request,
    invoice_id: str | None = Query(None),
    invoice: str | None = Query(None),
    account_id: str | None = Query(None),
    account: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_payment_forms_service.build_new_form_state(
        db,
        invoice_id=invoice_id,
        invoice_alias=invoice,
        account_id=account_id,
        account_alias=account,
    )
    selected_account = cast(Subscriber | None, state["selected_account"])
    prefill = cast(dict[str, Any], state["prefill"])
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            "accounts": None,
            "payment_methods": [],
            "payment_method_types": [],
            "collection_accounts": state["collection_accounts"],
            "invoices": state["invoices"],
            "prefill": prefill,
            "invoice_label": state["invoice_label"],
            "action_url": "/admin/billing/payments/create",
            "form_title": "Record Payment",
            "submit_label": "Record Payment",
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": bool(selected_account),
            "account_label": web_billing_payment_forms_service.account_label(selected_account) if selected_account else None,
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else None,
            "currency_locked": bool(prefill.get("invoice_id")),
            "show_invoice_typeahead": not bool(selected_account),
            "selected_invoice_id": prefill.get("invoice_id"),
            "balance_value": state["balance_value"],
            "balance_display": state["balance_display"],
        },
    )


@router.get(
    "/payments/invoice-options",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_invoice_options(
    request: Request,
    account_id: str | None = Query(None),
    invoice_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_payment_forms_service.load_invoice_options_state(
        db,
        account_id=account_id,
        invoice_id=invoice_id,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_invoice_select.html",
        {
            "request": request,
            "invoices": state["invoices"],
            "selected_invoice_id": invoice_id,
            "invoice_label": state["invoice_label"],
            "show_invoice_typeahead": not bool(state["selected_account"]),
        },
    )


@router.get(
    "/payments/invoice-currency",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_invoice_currency(
    request: Request,
    invoice_id: str | None = Query(None),
    currency: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_payment_forms_service.load_invoice_currency_state(
        db,
        invoice_id=invoice_id,
        currency=currency,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_currency_field.html",
        {
            "request": request,
            "currency_value": state["currency_value"],
            "currency_locked": state["currency_locked"],
        },
    )


@router.get(
    "/payments/invoice-details",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_invoice_details(
    request: Request,
    invoice_id: str | None = Query(None),
    amount: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_payment_forms_service.load_invoice_details_state(
        db,
        invoice_id=invoice_id,
        amount=amount,
    )
    return templates.TemplateResponse(
        "admin/billing/_payment_amount_field.html",
        {
            "request": request,
            "amount_value": state["amount_value"],
            "balance_value": state["balance_value"],
            "balance_display": state["balance_display"],
        },
    )


@router.get(
    "/customer-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def billing_customer_accounts(
    request: Request,
    customer_ref: str | None = Query(None),
    account_id: str | None = Query(None),
    account_x_model: str | None = Query(None),
    db: Session = Depends(get_db),
):
    accounts = web_billing_customers_service.accounts_for_customer(db, customer_ref)
    return templates.TemplateResponse(
        "admin/billing/_customer_account_select.html",
        {
            "request": request,
            "customer_ref": customer_ref,
            "accounts": accounts,
            "selected_account_id": account_id,
            "account_x_model": account_x_model,
        },
    )


@router.get(
    "/customer-subscribers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def billing_customer_subscribers(
    request: Request,
    customer_ref: str | None = Query(None),
    subscriber_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    subscribers = web_billing_customers_service.subscribers_for_customer(db, customer_ref)
    return templates.TemplateResponse(
        "admin/billing/_customer_subscriber_select.html",
        {
            "request": request,
            "customer_ref": customer_ref,
            "subscribers": subscribers,
            "selected_subscriber_id": subscriber_id,
        },
    )


@router.post("/payments/create", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_create(
    request: Request,
    account_id: str | None = Form(None),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    status: str | None = Form(None),
    invoice_id: str | None = Form(None),
    collection_account_id: str | None = Form(None),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    resolved_invoice = None
    balance_value = None
    balance_display = None
    try:
        result = web_billing_payments_service.process_payment_create(
            db,
            account_id=account_id,
            amount=amount,
            currency=currency,
            status=status,
            invoice_id=invoice_id,
            collection_account_id=collection_account_id,
            memo=memo,
        )
        payment = cast(Payment, result["payment"])
        resolved_invoice = result["resolved_invoice"]
        balance_value = result["balance_value"]
        balance_display = result["balance_display"]
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="create",
            entity_type="payment",
            entity_id=str(payment.id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "amount": str(payment.amount),
                "invoice_id": web_billing_payments_service.payment_primary_invoice_id(payment),
            },
        )
    except Exception as exc:
        deps = cast(
            dict[str, object],
            web_billing_payment_forms_service.load_create_error_dependencies(
                db,
                account_id=account_id,
                resolved_invoice=resolved_invoice,
            ),
        )
        error_state = web_billing_payment_forms_service.build_create_error_context(
            error=str(exc),
            deps=deps,
            resolved_invoice=resolved_invoice,
            invoice_id=invoice_id,
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/payment_form.html",
            {
                "request": request,
                **error_state,
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                "balance_value": balance_value,
                "balance_display": balance_display,
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/payments", status_code=303)


@router.get("/payments/{payment_id:uuid}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payment_detail(request: Request, payment_id: UUID, db: Session = Depends(get_db)) -> HTMLResponse:
    state = web_billing_payments_service.build_payment_detail_data(db, payment_id=str(payment_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_detail.html",
        {
            "request": request,
            **state,
            "activities": build_audit_activities(
                db, "payment", str(payment_id), limit=10
            ),
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/{payment_id:uuid}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_edit(request: Request, payment_id: UUID, db: Session = Depends(get_db)) -> HTMLResponse:
    state = web_billing_payments_service.build_payment_edit_data(db, payment_id=str(payment_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment not found"},
            status_code=404,
        )
    payment = cast(Payment, state["payment"])
    selected_account = cast(Subscriber | None, state["selected_account"])
    primary_invoice_id = state["primary_invoice_id"]
    deps = cast(dict[str, Any], state["deps"])
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_form.html",
        {
            "request": request,
            "accounts": None,
            "payment_methods": deps["payment_methods"],
            "payment_method_types": deps["payment_method_types"],
            "invoices": deps["invoices"],
            "payment": payment,
            "action_url": f"/admin/billing/payments/{payment_id}/edit",
            "form_title": "Edit Payment",
            "submit_label": "Save Changes",
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "account_locked": True,
            "account_label": web_billing_payment_forms_service.account_label(selected_account),
            "account_number": selected_account.account_number if selected_account else None,
            "selected_account_id": str(selected_account.id) if selected_account else str(payment.account_id),
            "currency_locked": bool(primary_invoice_id),
            "show_invoice_typeahead": False,
            "selected_invoice_id": primary_invoice_id,
            "balance_value": deps["balance_value"],
            "balance_display": deps["balance_display"],
        },
    )


@router.post("/payments/{payment_id:uuid}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_update(
    request: Request,
    payment_id: UUID,
    account_id: str | None = Form(None),
    amount: str = Form(...),
    currency: str = Form("NGN"),
    status: str | None = Form(None),
    invoice_id: str | None = Form(None),
    payment_method_id: str | None = Form(None),
    memo: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        result = web_billing_payments_service.process_payment_update(
            db,
            payment_id=str(payment_id),
            account_id=account_id,
            amount=amount,
            currency=currency,
            status=status,
            invoice_id=invoice_id,
            payment_method_id=payment_method_id,
            memo=memo,
        )
        before = result["before"]
        after = result["after"]
        metadata_payload = build_changes_metadata(before, after)
        from app.web.admin import get_current_user
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="payment",
            entity_id=str(payment_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
    except Exception as exc:
        edit_state = web_billing_payments_service.build_payment_edit_data(
            db, payment_id=str(payment_id)
        )
        payment = edit_state["payment"] if edit_state else None
        selected_account = edit_state["selected_account"] if edit_state else None
        deps = cast(dict[str, object], edit_state["deps"]) if edit_state else {}
        error_state = web_billing_payment_forms_service.build_edit_error_context(
            payment=payment,
            payment_id=payment_id,
            error=str(exc),
            deps=deps,
            selected_account=selected_account,
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/payment_form.html",
            {
                "request": request,
                **error_state,
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/billing/payments", status_code=303)


@router.get("/payments/import", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def payment_import_page(
    request: Request,
    history_handler: str | None = Query(None),
    history_status: str | None = Query(None),
    history_date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Bulk payment import page."""
    from app.web.admin import get_current_user, get_sidebar_stats
    import_history = web_billing_payments_service.list_payment_import_history_filtered(
        db,
        limit=100,
        handler=history_handler,
        status=history_status,
        date_range=history_date_range,
    )
    return templates.TemplateResponse(
        "admin/billing/payment_import.html",
        {
            "request": request,
            "active_page": "payments",
            "active_menu": "billing",
            "import_history": import_history,
            "history_handler": history_handler,
            "history_status": history_status,
            "history_date_range": history_date_range,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/reconciliation", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payment_reconciliation_page(
    request: Request,
    date_range: str | None = Query(None),
    handler: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    state = web_billing_reconciliation_service.build_reconciliation_data(
        db,
        date_range=date_range,
        handler=handler,
    )
    return templates.TemplateResponse(
        "admin/billing/payment_reconciliation.html",
        {
            "request": request,
            "active_page": "payments",
            "active_menu": "billing",
            **state,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/import/history.csv", dependencies=[Depends(require_permission("billing:read"))])
def payment_import_history_csv(
    request: Request,
    history_handler: str | None = Query(None),
    history_status: str | None = Query(None),
    history_date_range: str | None = Query(None),
    db: Session = Depends(get_db),
):
    rows = web_billing_payments_service.list_payment_import_history_filtered(
        db,
        limit=1000,
        handler=history_handler,
        status=history_status,
        date_range=history_date_range,
    )
    content = web_billing_payments_service.render_payment_import_history_csv(rows)
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="payment_import_history.csv"'},
    )


@router.post("/payments/import", dependencies=[Depends(require_permission("billing:write"))])
def payment_import_submit(
    request: Request,
    body: dict = Depends(parse_json_body),
    db: Session = Depends(get_db),
):
    """Process bulk payment import from JSON."""
    from fastapi.responses import JSONResponse

    from app.web.admin import get_current_user

    try:
        payments_data = body.get("payments", [])
        handler = body.get("handler")

        if not payments_data:
            return JSONResponse({"message": "No payments to import"}, status_code=400)

        default_currency = web_billing_payments_service.resolve_default_currency(db)
        normalized_rows = web_billing_payments_service.normalize_import_rows(
            payments_data,
            handler,
        )
        payment_source = body.get("payment_source")
        payment_method_type = body.get("payment_method_type")
        file_name = body.get("file_name")
        pair_inactive_customers = bool(body.get("pair_inactive_customers", True))
        row_count = len(normalized_rows)
        total_amount = 0.0
        for row in normalized_rows:
            try:
                total_amount += float(Decimal(str(row.get("amount", 0) or 0)))
            except (TypeError, ValueError, InvalidOperation):
                continue

        imported_count, errors = web_billing_payments_service.import_payments(
            db,
            normalized_rows,
            default_currency,
            payment_source=payment_source,
            payment_method_type=payment_method_type,
            pair_inactive_customers=pair_inactive_customers,
        )

        # Log audit event
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="import",
            entity_type="payment",
            entity_id="bulk",
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata={
                "imported": imported_count,
                "errors": len(errors),
                "payment_source": payment_source,
                "payment_method_type": payment_method_type,
                "file_name": file_name,
                "row_count": row_count,
                "total_amount": total_amount,
                "pair_inactive_customers": pair_inactive_customers,
                "handler": handler,
            },
        )

        return JSONResponse(
            web_billing_payments_service.build_import_result_payload(
                imported_count=imported_count,
                errors=errors,
            )
        )

    except Exception as exc:
        return JSONResponse({"message": f"Import failed: {str(exc)}"}, status_code=500)


@router.get("/payments/import/template", dependencies=[Depends(require_permission("billing:read"))])
def payment_import_template():
    """Download CSV template for payment import."""
    from fastapi.responses import Response

    return Response(
        content=web_billing_payments_service.import_template_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payment_import_template.csv"},
    )


@router.get("/accounts", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def accounts_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """List all billing accounts."""
    state = web_billing_accounts_service.build_accounts_list_data(
        db,
        page=page,
        per_page=per_page,
        customer_ref=customer_ref,
    )

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/accounts.html",
        {
            "request": request,
            **state,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/accounts/new", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_new(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    customer_ref = request.query_params.get("customer_ref")
    form_data = web_billing_accounts_service.build_account_form_data(
        db,
        customer_ref=customer_ref,
    )

    return templates.TemplateResponse(
        "admin/billing/account_form.html",
        {
            "request": request,
            "action_url": "/admin/billing/accounts",
            "form_title": "New Billing Account",
            "submit_label": "Create Account",
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            **form_data,
        },
    )


@router.post("/accounts", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_create(
    request: Request,
    subscriber_id: str | None = Form(None),
    customer_ref: str | None = Form(None),
    reseller_id: str | None = Form(None),
    tax_rate_id: str | None = Form(None),
    account_number: str | None = Form(None),
    status: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user
    try:
        account, selected_subscriber_id = web_billing_accounts_service.create_account_from_form(
            db,
            subscriber_id=subscriber_id,
            customer_ref=customer_ref,
            reseller_id=reseller_id,
            tax_rate_id=tax_rate_id,
            account_number=account_number,
            status=status,
            notes=notes,
        )
    except Exception as exc:
        from app.web.admin import get_current_user, get_sidebar_stats
        form_data = web_billing_accounts_service.build_account_form_data(
            db,
            customer_ref=customer_ref,
        )
        return templates.TemplateResponse(
            "admin/billing/account_form.html",
            {
                "request": request,
                "action_url": "/admin/billing/accounts",
                "form_title": "New Billing Account",
                "submit_label": "Create Account",
                "error": str(exc),
                "active_page": "accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                **form_data,
                "selected_subscriber_id": selected_subscriber_id if "selected_subscriber_id" in locals() else subscriber_id,
            },
            status_code=400,
        )
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="subscriber_account",
        entity_id=str(account.id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "account_number": account.account_number,
            "subscriber_id": str(account.id),
            "reseller_id": reseller_id or None,
        },
    )
    return RedirectResponse(url=f"/admin/billing/accounts/{account.id}", status_code=303)


@router.get("/accounts/{account_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_edit(request: Request, account_id: UUID, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats

    account = subscriber_service.accounts.get(db, str(account_id))
    customer_ref = (
        f"organization:{account.organization_id}" if account.organization_id else f"person:{account.id}"
    )
    form_data = web_billing_accounts_service.build_account_form_data(
        db,
        customer_ref=customer_ref,
    )
    return templates.TemplateResponse(
        "admin/billing/account_form.html",
        {
            "request": request,
            "action_url": f"/admin/billing/accounts/{account_id}/edit",
            "form_title": "Edit Billing Account",
            "submit_label": "Update Account",
            "active_page": "accounts",
            "active_menu": "billing",
            "account": account,
            "selected_subscriber_id": str(account.id),
            "selected_reseller_id": str(account.reseller_id) if account.reseller_id else "",
            "selected_tax_rate_id": str(account.tax_rate_id) if account.tax_rate_id else "",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            **form_data,
        },
    )


@router.post("/accounts/{account_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def account_update(
    request: Request,
    account_id: UUID,
    reseller_id: str | None = Form(None),
    tax_rate_id: str | None = Form(None),
    account_number: str | None = Form(None),
    status: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats

    before = subscriber_service.accounts.get(db, str(account_id))
    try:
        account = web_billing_accounts_service.update_account_from_form(
            db,
            account_id=str(account_id),
            reseller_id=reseller_id,
            tax_rate_id=tax_rate_id,
            account_number=account_number,
            status=status,
            notes=notes,
        )
        after = subscriber_service.accounts.get(db, str(account_id))
        metadata_payload = build_changes_metadata(before, after)
        current_user = get_current_user(request)
        log_audit_event(
            db=db,
            request=request,
            action="update",
            entity_type="subscriber_account",
            entity_id=str(account_id),
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
            metadata=metadata_payload,
        )
        return RedirectResponse(url=f"/admin/billing/accounts/{account.id}", status_code=303)
    except Exception as exc:
        customer_ref = (
            f"organization:{before.organization_id}" if before.organization_id else f"person:{before.id}"
        )
        form_data = web_billing_accounts_service.build_account_form_data(
            db,
            customer_ref=customer_ref,
        )
        return templates.TemplateResponse(
            "admin/billing/account_form.html",
            {
                "request": request,
                "action_url": f"/admin/billing/accounts/{account_id}/edit",
                "form_title": "Edit Billing Account",
                "submit_label": "Update Account",
                "error": str(exc),
                "active_page": "accounts",
                "active_menu": "billing",
                "account": before,
                "selected_subscriber_id": str(before.id),
                "selected_reseller_id": reseller_id or (str(before.reseller_id) if before.reseller_id else ""),
                "selected_tax_rate_id": tax_rate_id or (str(before.tax_rate_id) if before.tax_rate_id else ""),
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
                **form_data,
            },
            status_code=400,
        )


@router.get("/accounts/{account_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def account_detail(
    request: Request,
    account_id: UUID,
    statement_start: str | None = Query(None),
    statement_end: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(db, account_id=str(account_id))
    statement_range = web_billing_statements_service.parse_statement_range(statement_start, statement_end)
    statement = web_billing_statements_service.build_account_statement(
        db,
        account_id=account_id,
        date_range=statement_range,
    )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/account_detail.html",
        {
            "request": request,
            **state,
            "activities": build_audit_activities(
                db, "subscriber_account", str(account_id), limit=10
            ),
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "statement_range": statement_range,
            "statement": statement,
        },
    )


@router.get(
    "/accounts/{account_id}/statement.csv",
    dependencies=[Depends(require_permission("billing:read"))],
)
def account_statement_csv(
    account_id: UUID,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(db, account_id=str(account_id))
    account = state["account"]
    date_range = web_billing_statements_service.parse_statement_range(start_date, end_date)
    statement = web_billing_statements_service.build_account_statement(
        db,
        account_id=account_id,
        date_range=date_range,
    )
    account_label = (
        account.account_number
        or (account.organization.name if getattr(account, "organization", None) else "")
        or f"Account {str(account.id)[:8]}"
    )
    content = web_billing_statements_service.render_statement_csv(
        account_label=account_label,
        account_id=account_id,
        date_range=date_range,
        statement=statement,
    )
    filename = f"statement_{account_label.replace(' ', '_')}_{date_range.start_date.isoformat()}_{date_range.end_date.isoformat()}.csv"
    headers = {"Content-Disposition": build_content_disposition(filename)}
    return StreamingResponse(iter([content]), media_type="text/csv", headers=headers)


@router.post(
    "/accounts/{account_id}/statement/send",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def account_statement_send(
    request: Request,
    account_id: UUID,
    start_date: str | None = Form(None),
    end_date: str | None = Form(None),
    recipient_email: str | None = Form(None),
    db: Session = Depends(get_db),
):
    state = web_billing_accounts_service.build_account_detail_data(db, account_id=str(account_id))
    account = state["account"]
    date_range = web_billing_statements_service.parse_statement_range(start_date, end_date)
    statement = web_billing_statements_service.build_account_statement(
        db,
        account_id=account_id,
        date_range=date_range,
    )
    to_email = (recipient_email or account.email or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="No recipient email set for this account")
    notification_service.notifications.create(
        db,
        NotificationCreate(
            channel=NotificationChannel.email,
            recipient=to_email,
            status=NotificationStatus.queued,
            subject=f"Account statement ({date_range.start_date.isoformat()} - {date_range.end_date.isoformat()})",
            body=(
                "Your account statement is ready.\n\n"
                f"Period: {date_range.start_date.isoformat()} to {date_range.end_date.isoformat()}\n"
                f"Opening balance: {statement['opening_balance']:.2f}\n"
                f"Closing balance: {statement['closing_balance']:.2f}\n"
                f"Transactions: {len(statement['rows'])}\n"
            ),
        ),
    )
    return RedirectResponse(
        url=f"/admin/billing/accounts/{account_id}?statement_start={date_range.start_date.isoformat()}&statement_end={date_range.end_date.isoformat()}",
        status_code=303,
    )


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


@router.get("/dunning", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def billing_dunning(
    request: Request,
    page: int = 1,
    status: str | None = None,
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user, get_sidebar_stats
    state = web_billing_dunning_service.build_listing_data(
        db,
        page=page,
        status=status,
        customer_ref=customer_ref,
    )
    return templates.TemplateResponse(
        "admin/billing/dunning.html",
        {
            "request": request,
            **state,
            "active_page": "dunning",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post("/dunning/{case_id}/pause", dependencies=[Depends(require_permission("billing:write"))])
def dunning_pause(request: Request, case_id: str, db: Session = Depends(get_db)):
    """Pause a dunning case."""
    from app.web.admin import get_current_user
    web_billing_dunning_service.apply_case_action(db, case_id=case_id, action="pause")
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="pause",
        entity_type="dunning_case",
        entity_id=case_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/{case_id}/resume", dependencies=[Depends(require_permission("billing:write"))])
def dunning_resume(request: Request, case_id: str, db: Session = Depends(get_db)):
    """Resume a paused dunning case."""
    from app.web.admin import get_current_user
    web_billing_dunning_service.apply_case_action(db, case_id=case_id, action="resume")
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="resume",
        entity_type="dunning_case",
        entity_id=case_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/{case_id}/close", dependencies=[Depends(require_permission("billing:write"))])
def dunning_close(request: Request, case_id: str, db: Session = Depends(get_db)):
    """Close a dunning case."""
    from app.web.admin import get_current_user
    web_billing_dunning_service.apply_case_action(db, case_id=case_id, action="close")
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="close",
        entity_type="dunning_case",
        entity_id=case_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/bulk/pause", dependencies=[Depends(require_permission("billing:write"))])
def dunning_bulk_pause(request: Request, case_ids: str = Form(...), db: Session = Depends(get_db)):
    """Pause multiple dunning cases."""
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    processed_ids = web_billing_dunning_service.apply_bulk_action(
        db,
        case_ids_csv=case_ids,
        action="pause",
    )
    for case_id in processed_ids:
        log_audit_event(
            db=db,
            request=request,
            action="pause",
            entity_type="dunning_case",
            entity_id=case_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


@router.post("/dunning/bulk/resume", dependencies=[Depends(require_permission("billing:write"))])
def dunning_bulk_resume(request: Request, case_ids: str = Form(...), db: Session = Depends(get_db)):
    """Resume multiple paused dunning cases."""
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    processed_ids = web_billing_dunning_service.apply_bulk_action(
        db,
        case_ids_csv=case_ids,
        action="resume",
    )
    for case_id in processed_ids:
        log_audit_event(
            db=db,
            request=request,
            action="resume",
            entity_type="dunning_case",
            entity_id=case_id,
            actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        )
    return RedirectResponse(url="/admin/billing/dunning", status_code=303)


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


@router.get(
    "/collection-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def collection_accounts_list(
    request: Request,
    show_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    state = web_billing_collection_accounts_service.list_data(
        db,
        show_inactive=show_inactive,
    )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/collection_accounts.html",
        {
            "request": request,
            **state,
            "active_page": "collection_accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/collection-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_create(
    request: Request,
    name: str = Form(...),
    account_type: str = Form("bank"),
    currency: str = Form("NGN"),
    bank_name: str | None = Form(None),
    account_last4: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_collection_account(
            db=db,
            name=name,
            account_type=account_type,
            currency=currency,
            bank_name=bank_name,
            account_last4=account_last4,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)
    except Exception as exc:
        state = web_billing_collection_accounts_service.list_data(
            db,
            show_inactive=False,
        )
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/collection_accounts.html",
            {
                "request": request,
                **state,
                "error": str(exc),
                "active_page": "collection_accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/collection-accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_edit(request: Request, account_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_collection_accounts_service.edit_data(db, account_id=str(account_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Collection account not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/collection_account_form.html",
        {
            "request": request,
            **state,
            "action_url": f"/admin/billing/collection-accounts/{account_id}/edit",
            "form_title": "Edit Collection Account",
            "submit_label": "Update Account",
            "active_page": "collection_accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/collection-accounts/{account_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_update(
    request: Request,
    account_id: UUID,
    name: str = Form(...),
    account_type: str = Form("bank"),
    currency: str = Form("NGN"),
    bank_name: str | None = Form(None),
    account_last4: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_collection_account(
            db=db,
            account_id=account_id,
            name=name,
            account_type=account_type,
            currency=currency,
            bank_name=bank_name,
            account_last4=account_last4,
            notes=notes,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)
    except Exception as exc:
        state = web_billing_collection_accounts_service.edit_data(db, account_id=str(account_id))
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/collection_account_form.html",
            {
                "request": request,
                **(state or {}),
                "action_url": f"/admin/billing/collection-accounts/{account_id}/edit",
                "form_title": "Edit Collection Account",
                "submit_label": "Update Account",
                "error": str(exc),
                "active_page": "collection_accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/collection-accounts/{account_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_deactivate(account_id: UUID, db: Session = Depends(get_db)):
    billing_service.collection_accounts.delete(db, str(account_id))
    return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)


@router.post(
    "/collection-accounts/{account_id}/activate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def collection_accounts_activate(account_id: UUID, db: Session = Depends(get_db)):
    billing_service.collection_accounts.update(
        db,
        str(account_id),
        CollectionAccountUpdate(is_active=True),
    )
    return RedirectResponse(url="/admin/billing/collection-accounts", status_code=303)


@router.get(
    "/payment-channels",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_channels_list(request: Request, db: Session = Depends(get_db)):
    state = web_billing_channels_service.list_payment_channels_data(db)
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_channels.html",
        {
            "request": request,
            **state,
            "active_page": "payment_channels",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-channels",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_create(
    request: Request,
    name: str = Form(...),
    channel_type: str = Form("other"),
    provider_id: str | None = Form(None),
    default_collection_account_id: str | None = Form(None),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    fee_rules: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_payment_channel(
            db=db,
            name=name,
            channel_type=channel_type,
            provider_id=provider_id,
            default_collection_account_id=default_collection_account_id,
            is_default=is_default,
            is_active=is_active,
            fee_rules=fee_rules,
            notes=notes,
        )
        return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)
    except Exception as exc:
        state = web_billing_channels_service.list_payment_channels_data(db)
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/payment_channels.html",
            {
                "request": request,
                **state,
                "error": str(exc),
                "active_page": "payment_channels",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/payment-channels/{channel_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_edit(request: Request, channel_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_channels_service.load_payment_channel_edit_data(db, str(channel_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment channel not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_channel_form.html",
        {
            "request": request,
            **state,
            "action_url": f"/admin/billing/payment-channels/{channel_id}/edit",
            "form_title": "Edit Payment Channel",
            "submit_label": "Update Channel",
            "active_page": "payment_channels",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-channels/{channel_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_update(
    request: Request,
    channel_id: UUID,
    name: str = Form(...),
    channel_type: str = Form("other"),
    provider_id: str | None = Form(None),
    default_collection_account_id: str | None = Form(None),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    fee_rules: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_payment_channel(
            db=db,
            channel_id=channel_id,
            name=name,
            channel_type=channel_type,
            provider_id=provider_id,
            default_collection_account_id=default_collection_account_id,
            is_default=is_default,
            is_active=is_active,
            fee_rules=fee_rules,
            notes=notes,
        )
        return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)
    except Exception as exc:
        state = web_billing_channels_service.load_payment_channel_edit_data(db, str(channel_id))
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/payment_channel_form.html",
            {
                "request": request,
                **(state or {}),
                "action_url": f"/admin/billing/payment-channels/{channel_id}/edit",
                "form_title": "Edit Payment Channel",
                "submit_label": "Update Channel",
                "error": str(exc),
                "active_page": "payment_channels",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/payment-channels/{channel_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channels_deactivate(channel_id: UUID, db: Session = Depends(get_db)):
    billing_service.payment_channels.delete(db, str(channel_id))
    return RedirectResponse(url="/admin/billing/payment-channels", status_code=303)


@router.get(
    "/payment-channel-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_channel_accounts_list(request: Request, db: Session = Depends(get_db)):
    state = web_billing_channels_service.list_payment_channel_accounts_data(db)
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_channel_accounts.html",
        {
            "request": request,
            **state,
            "active_page": "payment_channel_accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-channel-accounts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_create(
    request: Request,
    channel_id: str = Form(...),
    collection_account_id: str = Form(...),
    currency: str | None = Form(None),
    priority: int = Form(0),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.create_payment_channel_account(
            db=db,
            channel_id=channel_id,
            collection_account_id=collection_account_id,
            currency=currency,
            priority=priority,
            is_default=is_default,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)
    except Exception as exc:
        state = web_billing_channels_service.list_payment_channel_accounts_data(db)
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/payment_channel_accounts.html",
            {
                "request": request,
                **state,
                "error": str(exc),
                "active_page": "payment_channel_accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/payment-channel-accounts/{mapping_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_edit(request: Request, mapping_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_channels_service.load_payment_channel_account_edit_data(db, str(mapping_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment channel mapping not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_channel_account_form.html",
        {
            "request": request,
            **state,
            "action_url": f"/admin/billing/payment-channel-accounts/{mapping_id}/edit",
            "form_title": "Edit Channel Mapping",
            "submit_label": "Update Mapping",
            "active_page": "payment_channel_accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-channel-accounts/{mapping_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_update(
    request: Request,
    mapping_id: UUID,
    channel_id: str = Form(...),
    collection_account_id: str = Form(...),
    currency: str | None = Form(None),
    priority: int = Form(0),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        billing_config_service.update_payment_channel_account(
            db=db,
            mapping_id=mapping_id,
            channel_id=channel_id,
            collection_account_id=collection_account_id,
            currency=currency,
            priority=priority,
            is_default=is_default,
            is_active=is_active,
        )
        return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)
    except Exception as exc:
        state = web_billing_channels_service.load_payment_channel_account_edit_data(db, str(mapping_id))
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/payment_channel_account_form.html",
            {
                "request": request,
                **(state or {}),
                "action_url": f"/admin/billing/payment-channel-accounts/{mapping_id}/edit",
                "form_title": "Edit Channel Mapping",
                "submit_label": "Update Mapping",
                "error": str(exc),
                "active_page": "payment_channel_accounts",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/payment-channel-accounts/{mapping_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_channel_accounts_deactivate(mapping_id: UUID, db: Session = Depends(get_db)):
    billing_service.payment_channel_accounts.delete(db, str(mapping_id))
    return RedirectResponse(url="/admin/billing/payment-channel-accounts", status_code=303)


# ---------------------------------------------------------------------------
# Payment Providers
# ---------------------------------------------------------------------------


@router.get(
    "/payment-providers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:read"))],
)
def payment_providers_list(
    request: Request,
    show_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    state = web_billing_providers_service.list_data(db, show_inactive=show_inactive)
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_providers.html",
        {
            "request": request,
            **state,
            "active_page": "payment_providers",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-providers",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_create(
    request: Request,
    name: str = Form(...),
    provider_type: str = Form("custom"),
    webhook_secret_ref: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        from app.models.billing import PaymentProviderType

        payload = PaymentProviderCreate(
            name=name.strip(),
            provider_type=PaymentProviderType(provider_type),
            webhook_secret_ref=webhook_secret_ref.strip() if webhook_secret_ref else None,
            notes=notes.strip() if notes else None,
            is_active=is_active is not None,
        )
        billing_service.payment_providers.create(db, payload)
        return RedirectResponse(url="/admin/billing/payment-providers", status_code=303)
    except Exception as exc:
        state = web_billing_providers_service.list_data(db, show_inactive=False)
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/payment_providers.html",
            {
                "request": request,
                **state,
                "error": str(exc),
                "active_page": "payment_providers",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.get(
    "/payment-providers/{provider_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_edit(
    request: Request,
    provider_id: UUID,
    db: Session = Depends(get_db),
):
    state = web_billing_providers_service.edit_data(db, provider_id=str(provider_id))
    if not state:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Payment provider not found"},
            status_code=404,
        )
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_provider_form.html",
        {
            "request": request,
            **state,
            "action_url": f"/admin/billing/payment-providers/{provider_id}/edit",
            "form_title": "Edit Payment Provider",
            "submit_label": "Update Provider",
            "active_page": "payment_providers",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.post(
    "/payment-providers/{provider_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_update(
    request: Request,
    provider_id: UUID,
    name: str = Form(...),
    provider_type: str = Form("custom"),
    webhook_secret_ref: str | None = Form(None),
    notes: str | None = Form(None),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        from app.models.billing import PaymentProviderType

        payload = PaymentProviderUpdate(
            name=name.strip(),
            provider_type=PaymentProviderType(provider_type),
            webhook_secret_ref=webhook_secret_ref.strip() if webhook_secret_ref else None,
            notes=notes.strip() if notes else None,
            is_active=is_active is not None,
        )
        billing_service.payment_providers.update(db, str(provider_id), payload)
        return RedirectResponse(url="/admin/billing/payment-providers", status_code=303)
    except Exception as exc:
        state = web_billing_providers_service.edit_data(db, provider_id=str(provider_id))
        from app.web.admin import get_current_user, get_sidebar_stats
        return templates.TemplateResponse(
            "admin/billing/payment_provider_form.html",
            {
                "request": request,
                **(state or {}),
                "action_url": f"/admin/billing/payment-providers/{provider_id}/edit",
                "form_title": "Edit Payment Provider",
                "submit_label": "Update Provider",
                "error": str(exc),
                "active_page": "payment_providers",
                "active_menu": "billing",
                "current_user": get_current_user(request),
                "sidebar_stats": get_sidebar_stats(db),
            },
            status_code=400,
        )


@router.post(
    "/payment-providers/{provider_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:write"))],
)
def payment_providers_deactivate(provider_id: UUID, db: Session = Depends(get_db)):
    billing_service.payment_providers.delete(db, str(provider_id))
    return RedirectResponse(url="/admin/billing/payment-providers", status_code=303)


# ---------------------------------------------------------------------------
# Payment Arrangements
# ---------------------------------------------------------------------------


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
