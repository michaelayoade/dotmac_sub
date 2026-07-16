"""Admin billing invoice bulk action routes."""

import json
import secrets
from typing import Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_invoice_bulk as web_billing_invoice_bulk_service
from app.services import web_billing_overview as web_billing_overview_service
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/billing", tags=["web-admin-billing"])
templates = Jinja2Templates(directory="templates")


def _require_confirmed_invoice_scope(
    db: Session,
    *,
    action: str,
    invoice_ids: str,
    confirmed: bool,
    expected_count: int | None,
    expected_scope_token: str | None,
) -> None:
    if not confirmed:
        raise HTTPException(
            status_code=400, detail="Invoice action confirmation required"
        )
    try:
        web_billing_invoice_bulk_service.require_invoice_bulk_confirmation(
            db,
            action=action,
            invoice_ids_csv=invoice_ids,
            expected_count=expected_count,
            expected_scope_token=expected_scope_token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/invoices/bulk/preview",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_bulk_preview(
    action: str = Form(...),
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        preview = web_billing_invoice_bulk_service.preview_invoice_bulk_action(
            db,
            action=action,
            invoice_ids_csv=invoice_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(preview.as_response())


@router.post(
    "/invoices/bulk/issue",
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_bulk_issue(
    request: Request,
    invoice_ids: str = Form(...),
    confirmed: bool = Form(False),
    expected_count: int | None = Form(None),
    expected_scope_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    _require_confirmed_invoice_scope(
        db,
        action="issue",
        invoice_ids=invoice_ids,
        confirmed=confirmed,
        expected_count=expected_count,
        expected_scope_token=expected_scope_token,
    )
    result = web_billing_invoice_bulk_service.execute_audited_bulk_action_result(
        db,
        request,
        action="issue",
        invoice_ids_csv=invoice_ids,
    )
    return JSONResponse(result.as_response("Issued"))


@router.post(
    "/invoices/bulk/send",
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_bulk_send(
    request: Request,
    invoice_ids: str = Form(...),
    confirmed: bool = Form(False),
    expected_count: int | None = Form(None),
    expected_scope_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    _require_confirmed_invoice_scope(
        db,
        action="send",
        invoice_ids=invoice_ids,
        confirmed=confirmed,
        expected_count=expected_count,
        expected_scope_token=expected_scope_token,
    )
    result = web_billing_invoice_bulk_service.execute_audited_bulk_action_result(
        db,
        request,
        action="send",
        invoice_ids_csv=invoice_ids,
    )
    return JSONResponse(result.as_response("Queued notifications for"))


@router.post(
    "/invoices/bulk/void",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def invoice_bulk_void(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    previews, skipped_ids = web_billing_invoice_bulk_service.preview_bulk_void(
        db, invoice_ids
    )
    return templates.TemplateResponse(
        "admin/billing/invoice_bulk_void_confirm.html",
        {
            "request": request,
            "previews": previews,
            "selected_ids": web_billing_invoice_bulk_service.parse_ids_csv(invoice_ids),
            "skipped_ids": skipped_ids,
            "preview_fingerprints_json": json.dumps(
                {str(preview.invoice_id): preview.fingerprint for preview in previews}
            ),
            "batch_key": f"admin-bulk-void-{secrets.token_urlsafe(18)}",
        },
    )


@router.post(
    "/invoices/bulk/void/confirm",
    dependencies=[Depends(require_permission("billing:invoice:delete"))],
)
def invoice_bulk_void_confirm(
    request: Request,
    invoice_ids: str = Form(...),
    preview_fingerprints_json: str = Form(...),
    batch_key: str = Form(...),
    db: Session = Depends(get_db),
):
    result = web_billing_invoice_bulk_service.confirm_bulk_void_result(
        db,
        invoice_ids_csv=invoice_ids,
        preview_fingerprints_json=preview_fingerprints_json,
        batch_key=batch_key,
    )
    return RedirectResponse(
        url=f"/admin/billing/invoices?notice={quote(result.message('Voided'))}",
        status_code=303,
    )


@router.post(
    "/invoices/bulk/mark-paid",
    dependencies=[Depends(require_permission("billing:invoice:update"))],
)
def invoice_bulk_mark_paid(
    request: Request,
    invoice_ids: str = Form(...),
    confirmed: bool = Form(False),
    expected_count: int | None = Form(None),
    expected_scope_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    _require_confirmed_invoice_scope(
        db,
        action="mark_paid",
        invoice_ids=invoice_ids,
        confirmed=confirmed,
        expected_count=expected_count,
        expected_scope_token=expected_scope_token,
    )
    result = web_billing_invoice_bulk_service.execute_audited_bulk_action_result(
        db,
        request,
        action="mark_paid",
        invoice_ids_csv=invoice_ids,
    )
    return JSONResponse(result.as_response("Marked paid"))


@router.post(
    "/invoices/bulk/generate-pdf",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_bulk_generate_pdf(
    request: Request,
    invoice_ids: str = Form(...),
    confirmed: bool = Form(False),
    expected_count: int | None = Form(None),
    expected_scope_token: str | None = Form(None),
    db: Session = Depends(get_db),
):
    _require_confirmed_invoice_scope(
        db,
        action="generate_pdf",
        invoice_ids=invoice_ids,
        confirmed=confirmed,
        expected_count=expected_count,
        expected_scope_token=expected_scope_token,
    )
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


@router.get(
    "/invoices/bulk/pdf-ready",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_bulk_pdf_ready(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    payload = web_billing_invoice_bulk_service.bulk_pdf_readiness(db, invoice_ids)
    return JSONResponse(payload)


@router.get(
    "/invoices/bulk/export.csv",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_bulk_export_csv(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    invoices = web_billing_invoice_bulk_service.list_invoices_by_ids(db, invoice_ids)
    content = web_billing_overview_service.render_invoices_csv(
        cast(list[Any], invoices)
    )
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="invoices_selected_export.csv"'
        },
    )


@router.get(
    "/invoices/bulk/export.zip",
    dependencies=[Depends(require_permission("billing:invoice:read"))],
)
def invoice_bulk_export_pdf_zip(
    invoice_ids: str = Query(""),
    db: Session = Depends(get_db),
):
    archive_buffer = web_billing_invoice_bulk_service.build_pdf_zip(db, invoice_ids)
    return StreamingResponse(
        archive_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="invoices_selected_pdfs.zip"'
        },
    )
