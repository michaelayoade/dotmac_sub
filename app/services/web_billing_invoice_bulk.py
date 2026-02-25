"""Service helpers for bulk invoice web actions."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.billing import InvoiceStatus
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services import billing as billing_service
from app.services import web_billing_invoices as web_billing_invoices_service


def parse_ids_csv(ids_csv: str) -> list[str]:
    """Parse comma-separated IDs into a cleaned list."""
    return [item.strip() for item in ids_csv.split(",") if item and item.strip()]


def list_invoices_by_ids(db, invoice_ids_csv: str):
    """Return invoices for the provided IDs, preserving input order."""
    invoices = []
    seen: set[str] = set()
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        if invoice_id in seen:
            continue
        seen.add(invoice_id)
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice:
                invoices.append(invoice)
        except Exception:
            continue
    return invoices


def bulk_issue(db, invoice_ids_csv: str) -> list[str]:
    """Issue draft invoices; return IDs that were updated."""
    updated: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice.status == InvoiceStatus.draft:
                invoice.status = InvoiceStatus.issued
                invoice.issued_at = datetime.now(UTC)
                db.commit()
                updated.append(invoice_id)
        except Exception:
            continue
    return updated


def bulk_send(db, invoice_ids_csv: str) -> list[str]:
    """Send invoice notifications for eligible invoices."""
    queued: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice:
                web_billing_invoices_service.maybe_send_invoice_notification(
                    db,
                    invoice=invoice,
                    send_notification="1",
                )
                queued.append(invoice_id)
        except Exception:
            continue
    return queued


def bulk_void(db, invoice_ids_csv: str) -> list[str]:
    """Void eligible invoices; return IDs that were updated."""
    updated: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice and invoice.status not in [InvoiceStatus.paid, InvoiceStatus.void]:
                invoice.status = InvoiceStatus.void
                db.commit()
                updated.append(invoice_id)
        except Exception:
            continue
    return updated


def bulk_mark_paid(db, invoice_ids_csv: str) -> list[str]:
    """Mark eligible invoices as paid; return IDs that were updated."""
    updated: list[str] = []
    eligible_statuses = {
        InvoiceStatus.issued,
        InvoiceStatus.overdue,
        InvoiceStatus.partially_paid,
    }
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if not invoice:
                continue
            if invoice.status not in eligible_statuses:
                continue
            invoice.status = InvoiceStatus.paid
            invoice.balance_due = 0
            invoice.paid_at = datetime.now(UTC)
            db.commit()
            updated.append(invoice_id)
        except Exception:
            continue
    return updated


def bulk_queue_pdf_exports(db, invoice_ids_csv: str, requested_by_id: str | None = None) -> dict[str, list[str]]:
    """Queue PDF exports for selected invoices and report results."""
    queued: list[str] = []
    ready: list[str] = []
    missing: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if not invoice:
                missing.append(invoice_id)
                continue
            latest_export = billing_invoice_pdf_service.get_latest_export(db, invoice_id=str(invoice.id))
            latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(db, latest_export)
            if billing_invoice_pdf_service.export_file_exists(db, latest_export):
                ready.append(str(invoice.id))
                continue
            billing_invoice_pdf_service.queue_export(
                db,
                invoice_id=str(invoice.id),
                requested_by_id=requested_by_id,
            )
            queued.append(str(invoice.id))
        except Exception:
            missing.append(invoice_id)
            continue
    return {"queued": queued, "ready": ready, "missing": missing}


def bulk_pdf_readiness(db, invoice_ids_csv: str) -> dict[str, object]:
    """Return readiness summary for selected invoice PDFs."""
    ready: list[str] = []
    pending: list[str] = []
    missing: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if not invoice:
                missing.append(invoice_id)
                continue
            latest_export = billing_invoice_pdf_service.get_latest_export(db, invoice_id=str(invoice.id))
            latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(db, latest_export)
            if billing_invoice_pdf_service.export_file_exists(db, latest_export):
                ready.append(str(invoice.id))
            else:
                pending.append(str(invoice.id))
        except Exception:
            missing.append(invoice_id)
            continue

    total = len(ready) + len(pending)
    return {
        "total": total,
        "ready_count": len(ready),
        "pending_count": len(pending),
        "missing_count": len(missing),
        "all_ready": total > 0 and len(pending) == 0,
        "ready": ready,
        "pending": pending,
        "missing": missing,
    }
