"""Service helpers for bulk invoice web actions."""

from __future__ import annotations

import io
import logging
import zipfile
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus
from app.services import billing as billing_service
from app.services import billing_invoice_pdf as billing_invoice_pdf_service
from app.services import web_billing_invoices as web_billing_invoices_service
from app.services.object_storage import ObjectNotFoundError

logger = logging.getLogger(__name__)


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


def execute_bulk_action(db, *, action: str, invoice_ids_csv: str) -> list[str]:
    """Execute a named bulk invoice action and return processed IDs."""
    if action == "issue":
        return bulk_issue(db, invoice_ids_csv)
    if action == "send":
        return bulk_send(db, invoice_ids_csv)
    if action == "void":
        return bulk_void(db, invoice_ids_csv)
    if action == "mark_paid":
        return bulk_mark_paid(db, invoice_ids_csv)
    raise ValueError("Unsupported invoice bulk action")


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


def build_pdf_zip(db: Session, invoice_ids_csv: str) -> io.BytesIO:
    """Build a ZIP archive containing PDF exports for the given invoices.

    Fetches the latest PDF export for each invoice, bundles them into a
    ZIP file with duplicate-filename resolution, and appends a README
    when invoices are skipped or the selection is empty.

    Args:
        db: Database session.
        invoice_ids_csv: Comma-separated invoice IDs.

    Returns:
        A BytesIO buffer containing the ZIP archive.
    """
    invoices = list_invoices_by_ids(db, invoice_ids_csv)
    archive_buffer = io.BytesIO()
    skipped: list[str] = []
    used_names: set[str] = set()

    with zipfile.ZipFile(
        archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for invoice in invoices:
            latest_export = billing_invoice_pdf_service.get_latest_export(
                db, invoice_id=str(invoice.id)
            )
            latest_export = billing_invoice_pdf_service.maybe_finalize_stalled_export(
                db, latest_export
            )
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
            logger.info("Bulk PDF ZIP skipped %d invoices", len(skipped))
            archive.writestr(
                "README.txt",
                "Some selected invoices were skipped because PDF exports were not ready:\n"
                + "\n".join(f"- {value}" for value in skipped),
            )
        elif not invoices:
            archive.writestr("README.txt", "No invoices were selected.")

    archive_buffer.seek(0)
    return archive_buffer


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
