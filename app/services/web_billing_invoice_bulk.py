"""Service helpers for bulk invoice web actions."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.billing import InvoiceStatus
from app.services import billing as billing_service


def parse_ids_csv(ids_csv: str) -> list[str]:
    """Parse comma-separated IDs into a cleaned list."""
    return [item.strip() for item in ids_csv.split(",") if item and item.strip()]


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
    """Return invoice IDs considered queued for send."""
    queued: list[str] = []
    for invoice_id in parse_ids_csv(invoice_ids_csv):
        try:
            invoice = billing_service.invoices.get(db, invoice_id)
            if invoice:
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
