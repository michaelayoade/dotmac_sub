"""Admin billing invoice bulk action routes."""

from typing import Any, cast

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_billing_invoice_bulk as web_billing_invoice_bulk_service
from app.services import web_billing_overview as web_billing_overview_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission

router = APIRouter(prefix="/billing", tags=["web-admin-billing"])


def _log_bulk_audit_events(
    *,
    db: Session,
    request: Request,
    action: str,
    entity_type: str,
    entity_ids: list[str],
    actor_id: str | None,
) -> None:
    for entity_id in entity_ids:
        log_audit_event(
            db=db,
            request=request,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_id=actor_id,
        )


@router.post("/invoices/bulk/issue", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_issue(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    updated_ids = web_billing_invoice_bulk_service.execute_bulk_action(
        db,
        action="issue",
        invoice_ids_csv=invoice_ids,
    )
    count = len(updated_ids)
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="issue",
        entity_type="invoice",
        entity_ids=updated_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return JSONResponse({"message": f"Issued {count} invoices", "count": count})


@router.post("/invoices/bulk/send", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_send(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    queued_ids = web_billing_invoice_bulk_service.execute_bulk_action(
        db,
        action="send",
        invoice_ids_csv=invoice_ids,
    )
    count = len(queued_ids)
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="send",
        entity_type="invoice",
        entity_ids=queued_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return JSONResponse({"message": f"Queued {count} invoice notifications", "count": count})


@router.post("/invoices/bulk/void", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_void(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    updated_ids = web_billing_invoice_bulk_service.execute_bulk_action(
        db,
        action="void",
        invoice_ids_csv=invoice_ids,
    )
    count = len(updated_ids)
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="void",
        entity_type="invoice",
        entity_ids=updated_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return JSONResponse({"message": f"Voided {count} invoices", "count": count})


@router.post("/invoices/bulk/mark-paid", dependencies=[Depends(require_permission("billing:write"))])
def invoice_bulk_mark_paid(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    updated_ids = web_billing_invoice_bulk_service.execute_bulk_action(
        db,
        action="mark_paid",
        invoice_ids_csv=invoice_ids,
    )
    count = len(updated_ids)
    _log_bulk_audit_events(
        db=db,
        request=request,
        action="mark_paid",
        entity_type="invoice",
        entity_ids=updated_ids,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
    )
    return JSONResponse({"message": f"Marked {count} invoices as paid", "count": count})


@router.post("/invoices/bulk/generate-pdf", dependencies=[Depends(require_permission("billing:read"))])
def invoice_bulk_generate_pdf(
    request: Request,
    invoice_ids: str = Form(...),
    db: Session = Depends(get_db),
):
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
    archive_buffer = web_billing_invoice_bulk_service.build_pdf_zip(db, invoice_ids)
    return StreamingResponse(
        archive_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="invoices_selected_pdfs.zip"'},
    )
