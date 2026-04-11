"""Service helpers for billing invoice action routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import InvoicePdfExport
from app.services import billing as billing_service
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services.file_storage import build_content_disposition
from app.services.object_storage import ObjectNotFoundError

logger = logging.getLogger(__name__)


def html_notice(message: str) -> str:
    return (
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 '
        'shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"{message}"
        "</div>"
    )


def pdf_message(invoice_id) -> str:
    return html_notice(f"PDF generation queued for invoice {invoice_id}.")


def send_message(invoice_id) -> str:
    return html_notice(f"Invoice {invoice_id} send queued.")


def void_message(invoice_id) -> str:
    return html_notice(f"Invoice {invoice_id} void queued.")


def batch_today_str() -> str:
    return datetime.now(UTC).date().strftime("%Y-%m-%d")


def regenerate_invoice_pdf(
    db: Session,
    *,
    invoice_id: UUID,
    requested_by_id: str | None,
) -> None:
    billing_invoice_pdf_service.regenerate_invoice_cache(
        db,
        invoice_id=str(invoice_id),
        requested_by_id=requested_by_id,
    )


def generate_invoice_pdf_export(
    db: Session,
    *,
    invoice_id: UUID,
    requested_by_id: str | None,
) -> InvoicePdfExport | None:
    export: InvoicePdfExport | None = billing_invoice_pdf_service.generate_export_now(
        db,
        invoice_id=str(invoice_id),
        requested_by_id=requested_by_id,
        force_new=False,
    )
    db.expire_all()
    return db.get(type(export), export.id) if export else None


def stream_pdf_export_response(db: Session, latest_export, invoice):
    from fastapi.responses import StreamingResponse

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


def cached_invoice_pdf_response(db: Session, *, invoice_id: UUID):
    invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
    if not invoice:
        return None, False

    db.expire_all()
    latest_export = billing_invoice_pdf_service.get_latest_export(
        db,
        invoice_id=str(invoice_id),
    )
    latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(
        db, latest_export
    )
    if billing_invoice_pdf_service.is_export_cache_valid(db, invoice, latest_export):
        billing_invoice_pdf_service.record_cache_hit(db)
        return stream_pdf_export_response(db, latest_export, invoice), True
    billing_invoice_pdf_service.record_cache_miss(db)
    return None, True


def invoice_exists(db: Session, *, invoice_id: UUID) -> bool:
    return billing_service.invoices.get(db=db, invoice_id=str(invoice_id)) is not None


def generated_pdf_response(db: Session, *, invoice_id: UUID, export):
    invoice = billing_service.invoices.get(db=db, invoice_id=str(invoice_id))
    if not invoice:
        return None
    if billing_invoice_pdf_service.is_export_cache_valid(db, invoice, export):
        return stream_pdf_export_response(db, export, invoice)
    return None


def pdf_notice_for_export(export: InvoicePdfExport) -> str:
    status_value = export.status.value
    if status_value == "processing":
        return "processing"
    if status_value == "failed":
        return "failed"
    return "queued"
