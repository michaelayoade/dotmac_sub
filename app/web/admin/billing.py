"""Admin billing management web routes."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.billing import CreditNoteStatus, Payment
from app.models.catalog import BillingCycle
from app.models.subscriber import Subscriber
from app.schemas.billing import (
    CollectionAccountUpdate,
    CreditNoteApplyRequest,
    CreditNoteCreate,
    InvoiceCreate,
    InvoiceLineCreate,
    InvoiceUpdate,
    TaxRateCreate,
)
from app.services import billing as billing_service
from app.services import web_billing_accounts as web_billing_accounts_service
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
from app.services import web_billing_tax_rates as web_billing_tax_rates_service
from app.services.audit_helpers import build_changes_metadata, log_audit_event
from app.services.auth_dependencies import require_permission
from app.services.billing import configuration as billing_config_service
from app.web.request_parsing import parse_json_body

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
    db: Session = Depends(get_db),
):
    """Billing overview page."""
    state = web_billing_overview_service.build_overview_data(db)

    # Get sidebar stats and current user
    from app.web.admin import get_current_user, get_sidebar_stats
    sidebar_stats = get_sidebar_stats(db)
    current_user = get_current_user(request)

    return templates.TemplateResponse(
        "admin/billing/index.html",
        {
            "request": request,
            **state,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
    )


@router.get("/invoices", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoices_list(
    request: Request,
    account_id: str | None = None,
    status: str | None = None,
    customer_ref: str | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """List all invoices with filtering."""
    state = web_billing_overview_service.build_invoices_list_data(
        db,
        account_id=account_id,
        status=status,
        customer_ref=customer_ref,
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
    account_id: str = Form(...),
    invoice_number: str | None = Form(None),
    status: str | None = Form(None),
    currency: str = Form("NGN"),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    memo: str | None = Form(None),
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
        payload_data = web_billing_invoices_service.build_invoice_payload_data(
            account_id=_parse_uuid(account_id, "account_id"),
            invoice_number=invoice_number,
            status=status,
            currency=currency,
            issued_at=_parse_datetime(issued_at),
            due_at=_parse_datetime(due_at),
            memo=memo,
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
            account_id=account_id,
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
    account_id: str = Form(...),
    invoice_number: str | None = Form(None),
    status: str | None = Form(None),
    currency: str = Form("NGN"),
    issued_at: str | None = Form(None),
    due_at: str | None = Form(None),
    memo: str | None = Form(None),
    line_items_json: str | None = Form(None),
    db: Session = Depends(get_db),
):
    try:
        before = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
        payload_data = web_billing_invoices_service.build_invoice_payload_data(
            account_id=_parse_uuid(account_id, "account_id"),
            invoice_number=invoice_number,
            status=status,
            currency=currency,
            issued_at=_parse_datetime(issued_at),
            due_at=_parse_datetime(due_at),
            memo=memo,
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
    db: Session = Depends(get_db),
):
    note = web_billing_invoice_batch_service.run_batch(
        db,
        billing_cycle=billing_cycle,
        parse_cycle_fn=_parse_billing_cycle,
    )
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"{note}"
        "</div>"
    )


@router.get("/invoices/batch", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_batch(request: Request, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user, get_sidebar_stats
    today = web_billing_invoice_actions_service.batch_today_str()
    return templates.TemplateResponse(
        "admin/billing/invoice_batch.html",
        {
            "request": request,
            "active_page": "invoices",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
            "today": today,
        },
    )


@router.post("/invoices/generate-batch/preview", dependencies=[Depends(require_permission("billing:read"))])
def invoice_generate_batch_preview(
    request: Request,
    billing_cycle: str | None = Form(None),
    subscription_status: str | None = Form(None),
    billing_date: str | None = Form(None),
    invoice_status: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Dry-run preview of batch invoice generation."""
    from fastapi.responses import JSONResponse

    try:
        payload = web_billing_invoice_batch_service.preview_batch(
            db=db,
            billing_cycle=billing_cycle,
            billing_date=billing_date,
            parse_cycle_fn=_parse_billing_cycle,
        )
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse(web_billing_invoice_batch_service.preview_error_payload(exc), status_code=400)


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_detail(
    request: Request,
    invoice_id: UUID,
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
    return HTMLResponse(web_billing_invoice_actions_service.pdf_message(invoice_id))


@router.post("/invoices/{invoice_id}/send", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
def invoice_send(request: Request, invoice_id: UUID, db: Session = Depends(get_db)):
    from app.web.admin import get_current_user
    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="send",
        entity_type="invoice",
        entity_id=str(invoice_id),
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return HTMLResponse(web_billing_invoice_actions_service.send_message(invoice_id))


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
    return RedirectResponse(url="/admin/billing/invoices", status_code=303)


@router.get("/payments", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def payments_list(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    customer_ref: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """List all payments."""
    state = web_billing_payments_service.build_payments_list_data(
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
        "admin/billing/payments.html",
        {
            "request": request,
            **state,
            "current_user": current_user,
            "sidebar_stats": sidebar_stats,
        },
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


@router.get("/payments/{payment_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
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
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
    )


@router.get("/payments/{payment_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
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


@router.post("/payments/{payment_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:write"))])
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
def payment_import_page(request: Request, db: Session = Depends(get_db)):
    """Bulk payment import page."""
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/payment_import.html",
        {
            "request": request,
            "active_page": "payments",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
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

        if not payments_data:
            return JSONResponse({"message": "No payments to import"}, status_code=400)

        default_currency = web_billing_payments_service.resolve_default_currency(db)

        imported_count, errors = web_billing_payments_service.import_payments(
            db, payments_data, default_currency
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
            metadata={"imported": imported_count, "errors": len(errors)},
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
    return RedirectResponse(url=f"/admin/billing/accounts/{account.id}", status_code=303)


@router.get("/accounts/{account_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def account_detail(request: Request, account_id: UUID, db: Session = Depends(get_db)):
    state = web_billing_accounts_service.build_account_detail_data(db, account_id=str(account_id))
    from app.web.admin import get_current_user, get_sidebar_stats
    return templates.TemplateResponse(
        "admin/billing/account_detail.html",
        {
            "request": request,
            **state,
            "active_page": "accounts",
            "active_menu": "billing",
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
        },
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
def billing_ar_aging(request: Request, db: Session = Depends(get_db)):
    state = web_billing_overview_service.build_ar_aging_data(db)
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
    db: Session = Depends(get_db),
):
    entry_type = request.query_params.get("entry_type")
    state = web_billing_ledger_service.build_ledger_entries_data(
        db,
        customer_ref=customer_ref,
        entry_type=entry_type,
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
