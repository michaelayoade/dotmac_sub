"""Admin billing invoice action/detail routes."""

from decimal import Decimal, InvalidOperation
from time import sleep
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import billing as billing_service
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services import web_billing_invoice_actions as web_billing_invoice_actions_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission
from app.services.file_storage import build_content_disposition
from app.services.object_storage import ObjectNotFoundError

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


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse, dependencies=[Depends(require_permission("billing:read"))])
def invoice_detail(
    request: Request,
    invoice_id: UUID,
    pdf_notice: str | None = Query(None),
    db: Session = Depends(get_db),
):
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

    from app.web.admin import get_current_user, get_sidebar_stats

    return templates.TemplateResponse(
        "admin/billing/invoice_detail.html",
        {
            "request": request,
            **detail_data,
            "pdf_notice": pdf_notice,
            "current_user": get_current_user(request),
            "sidebar_stats": get_sidebar_stats(db),
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
        web_billing_invoices_service.create_invoice_line_from_form(
            db,
            invoice_id=str(invoice_id),
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            tax_rate_id=tax_rate_id,
            parse_uuid=_parse_uuid,
            parse_decimal=_parse_decimal,
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
        metadata_payload = web_billing_invoices_service.apply_credit_note_to_invoice(
            db,
            invoice_id=str(invoice_id),
            credit_note_id=credit_note_id,
            amount=_parse_decimal(amount, "amount") if amount else None,
            memo=memo.strip() if memo else None,
        )
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
