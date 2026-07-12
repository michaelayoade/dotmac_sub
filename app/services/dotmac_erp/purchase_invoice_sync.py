"""Purchase-invoice origination and repair for the Sub -> ERP outbox."""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    flow_owned_by_sub,
)
from app.models.vendor_routes import (
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceStatus,
)
from app.services.dotmac_erp import outbox
from app.services.dotmac_erp.client import build_erp_client
from app.services.file_storage import file_uploads

logger = logging.getLogger(__name__)

ENTITY_TYPE = "vendor_purchase_invoice"


def purchase_invoice_idempotency_key(invoice: VendorPurchaseInvoice) -> str:
    return f"pinv-{invoice.id}"


def purchase_invoice_eligibility_error(invoice: VendorPurchaseInvoice) -> str | None:
    if invoice.status != VendorPurchaseInvoiceStatus.approved.value:
        return "Purchase invoice is not approved"
    if invoice.erp_purchase_invoice_id:
        return "Purchase invoice is already linked to ERP"
    if invoice.project is None or invoice.project.project is None:
        return "Purchase invoice project context is missing"
    if invoice.vendor is None:
        return "Purchase invoice vendor context is missing"
    if not (invoice.vendor.erp_id or "").strip():
        return "Vendor is not linked to an ERP supplier"
    erp_po_id = (
        invoice.erp_purchase_order_id
        or invoice.project.erp_purchase_order_id
        or ""
    ).strip()
    if not erp_po_id:
        return "Waiting for the installation project's ERP purchase order"
    if not any(item.is_active and item.quantity > 0 for item in invoice.line_items):
        return "Purchase invoice has no active, positive-quantity line items"
    return None


def build_purchase_invoice_payload(invoice: VendorPurchaseInvoice) -> dict:
    reason = purchase_invoice_eligibility_error(invoice)
    if reason:
        raise ValueError(reason)

    project = invoice.project
    base_project = project.project
    vendor = invoice.vendor
    erp_po_id = (
        invoice.erp_purchase_order_id or project.erp_purchase_order_id or ""
    ).strip()
    items = []
    for item in invoice.line_items:
        if not item.is_active or item.quantity <= 0:
            continue
        item_type = (item.item_type or "").strip() or "item"
        description = (item.description or "").strip()
        items.append(
            {
                "item_type": item_type[:80],
                "description": (
                    description
                    or f"{item_type.replace('_', ' ').title()} item"
                )[:2000],
                "quantity": str(item.quantity),
                "unit_price": str(item.unit_price),
                "amount": str(item.amount),
                "notes": item.notes,
            }
        )

    payload = {
        "source_invoice_id": str(invoice.id),
        "source_invoice_number": invoice.invoice_number,
        "source_project_id": str(base_project.id),
        "installation_project_id": str(project.id),
        "source_quote_id": (
            str(project.approved_quote_id) if project.approved_quote_id else None
        ),
        "erp_purchase_order_id": erp_po_id,
        "vendor_name": vendor.name,
        "vendor_erp_id": vendor.erp_id,
        "vendor_code": (vendor.code or vendor.name)[:160],
        "currency": invoice.currency,
        "tax_rate_percent": str(invoice.tax_rate_percent or 0),
        "subtotal": str(invoice.subtotal),
        "tax_total": str(invoice.tax_total),
        "total": str(invoice.total),
        "items": items,
    }
    if base_project.code:
        payload["project_code"] = base_project.code
    if base_project.name:
        payload["project_name"] = base_project.name
    if invoice.reviewed_at:
        payload["approved_at"] = invoice.reviewed_at.isoformat()
    if invoice.reviewed_by and invoice.reviewed_by.email:
        payload["approved_by_email"] = invoice.reviewed_by.email
    return payload


def enqueue_purchase_invoice(
    db: Session, invoice: VendorPurchaseInvoice
) -> FieldErpSyncEvent | None:
    """Queue a new-only invoice only after this flow has moved to Sub."""
    if not flow_owned_by_sub(db, FieldErpSyncFlow.purchase_invoice):
        return None
    reason = purchase_invoice_eligibility_error(invoice)
    if reason:
        invoice.erp_sync_error = reason[:500]
        return None
    invoice.erp_purchase_order_id = invoice.project.erp_purchase_order_id
    invoice.erp_sync_error = None
    return outbox.enqueue(
        db,
        flow=FieldErpSyncFlow.purchase_invoice,
        entity_type=ENTITY_TYPE,
        entity_id=invoice.id,
        idempotency_key=purchase_invoice_idempotency_key(invoice),
        payload=build_purchase_invoice_payload(invoice),
    )


def event_ready(db: Session, event: FieldErpSyncEvent) -> bool:
    """Return false while a queued invoice is waiting for its ERP PO."""
    invoice = db.get(VendorPurchaseInvoice, event.entity_id)
    if invoice is None:
        return True  # Let normal delivery dead-letter the invalid source.
    erp_po_id = invoice.erp_purchase_order_id or invoice.project.erp_purchase_order_id
    if not erp_po_id:
        invoice.erp_sync_error = "Waiting for the installation project's ERP purchase order"
        return False
    if event.payload.get("erp_purchase_order_id") != erp_po_id:
        invoice.erp_purchase_order_id = erp_po_id
        event.payload = build_purchase_invoice_payload(invoice)
    return True


def _extract_erp_invoice_id(response: dict | None) -> str | None:
    if not isinstance(response, dict):
        return None
    value = (
        response.get("purchase_invoice_id")
        or response.get("invoice_id")
        or response.get("name")
    )
    return str(value) if value else None


def apply_erp_response(db: Session, event: FieldErpSyncEvent) -> None:
    invoice = db.get(VendorPurchaseInvoice, event.entity_id)
    if invoice is None:
        logger.warning("No vendor purchase invoice for ERP event %s", event.id)
        return
    erp_id = _extract_erp_invoice_id(event.erp_response)
    if not erp_id:
        invoice.erp_sync_error = "ERP response did not include a purchase invoice ID"
        return
    invoice.erp_purchase_invoice_id = erp_id[:100]
    invoice.erp_purchase_invoice_status = str(
        (event.erp_response or {}).get("status") or "created"
    )[:40]
    invoice.erp_sync_error = None
    invoice.erp_synced_at = datetime.now(UTC)


def upload_attachment(db: Session, invoice: VendorPurchaseInvoice) -> bool:
    if invoice.attachment is None or invoice.attachment.is_deleted:
        return False
    if not invoice.erp_purchase_invoice_id or invoice.erp_attachment_synced_at:
        return False
    stream = file_uploads.stream_file(invoice.attachment)
    data = b"".join(stream.chunks)
    payload = {
        "file_name": invoice.attachment.original_filename,
        "mime_type": invoice.attachment.content_type or "application/octet-stream",
        "content_base64": base64.b64encode(data).decode("ascii"),
    }
    with build_erp_client(db) as client:
        client.upload_purchase_invoice_attachment(
            invoice.erp_purchase_invoice_id,
            payload,
            idempotency_key=f"pinv-attach-{invoice.id}",
        )
    invoice.erp_attachment_synced_at = datetime.now(UTC)
    invoice.erp_sync_error = None
    return True


def repair_purchase_invoice_sync(db: Session, *, limit: int = 100) -> dict:
    """Queue newly eligible invoices and retry post-create attachments."""
    rows = (
        db.query(VendorPurchaseInvoice)
        .filter(VendorPurchaseInvoice.is_active.is_(True))
        .filter(
            VendorPurchaseInvoice.status
            == VendorPurchaseInvoiceStatus.approved.value
        )
        .order_by(VendorPurchaseInvoice.updated_at.asc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    processed = 0
    enqueued = 0
    attachments = 0
    errors: list[str] = []
    for invoice in rows:
        processed += 1
        try:
            if not invoice.erp_purchase_invoice_id:
                if enqueue_purchase_invoice(db, invoice) is not None:
                    enqueued += 1
            elif upload_attachment(db, invoice):
                attachments += 1
            db.commit()
        except Exception as exc:  # Each invoice remains independently retryable.
            db.rollback()
            current = db.get(VendorPurchaseInvoice, invoice.id)
            if current is not None:
                current.erp_sync_error = str(exc)[:500]
                db.commit()
            errors.append(f"{invoice.id}: {exc}")
    return {
        "processed": processed,
        "enqueued": enqueued,
        "attachments": attachments,
        "errors": errors,
    }
