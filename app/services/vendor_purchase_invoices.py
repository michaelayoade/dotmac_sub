"""Native vendor purchase-invoice workflow and attachment handling."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.vendor_routes import (
    InstallationProject,
    ProjectQuote,
    ProjectQuoteStatus,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceLineItem,
    VendorPurchaseInvoiceStatus,
)
from app.schemas.vendor_purchase_invoice import (
    VendorPurchaseInvoiceCreate,
    VendorPurchaseInvoiceLineCreate,
    VendorPurchaseInvoiceLineUpdate,
    VendorPurchaseInvoiceUpdate,
)
from app.services.common import apply_pagination, coerce_uuid
from app.services.file_storage import FileValidationError, file_uploads

_MONEY = Decimal("0.01")
_EDITABLE = {
    VendorPurchaseInvoiceStatus.draft.value,
    VendorPurchaseInvoiceStatus.revision_requested.value,
}
_REVIEWABLE = {
    VendorPurchaseInvoiceStatus.submitted.value,
    VendorPurchaseInvoiceStatus.under_review.value,
}


def _money(value: Decimal | int | str | None) -> Decimal:
    return Decimal(str(value or "0")).quantize(_MONEY, rounding=ROUND_HALF_UP)


def _query(db: Session):
    return db.query(VendorPurchaseInvoice).options(
        selectinload(VendorPurchaseInvoice.line_items),
        selectinload(VendorPurchaseInvoice.attachment),
        selectinload(VendorPurchaseInvoice.project).selectinload(
            InstallationProject.project
        ),
        selectinload(VendorPurchaseInvoice.vendor),
    )


def _get(db: Session, invoice_id: str) -> VendorPurchaseInvoice:
    invoice = (
        _query(db)
        .filter(VendorPurchaseInvoice.id == coerce_uuid(invoice_id))
        .filter(VendorPurchaseInvoice.is_active.is_(True))
        .one_or_none()
    )
    if invoice is None:
        raise HTTPException(status_code=404, detail="Purchase invoice not found")
    return invoice


def _assert_vendor(invoice: VendorPurchaseInvoice, vendor_id: str | None) -> None:
    if vendor_id and str(invoice.vendor_id) != str(vendor_id):
        raise HTTPException(status_code=404, detail="Purchase invoice not found")


def _assert_editable(invoice: VendorPurchaseInvoice) -> None:
    if invoice.status not in _EDITABLE:
        raise HTTPException(
            status_code=409,
            detail="Only draft or revision-requested invoices can be edited",
        )


def _project_for_vendor(
    db: Session, project_id: str, vendor_id: str
) -> InstallationProject:
    project = db.get(InstallationProject, coerce_uuid(project_id))
    if project is None or not project.is_active:
        raise HTTPException(status_code=404, detail="Installation project not found")
    has_quote = (
        db.query(ProjectQuote.id)
        .filter(ProjectQuote.project_id == project.id)
        .filter(ProjectQuote.vendor_id == coerce_uuid(vendor_id))
        .filter(ProjectQuote.is_active.is_(True))
        .first()
        is not None
    )
    if str(project.assigned_vendor_id or "") != str(vendor_id) and not has_quote:
        raise HTTPException(status_code=404, detail="Installation project not found")
    return project


def _has_submitted_quote(db: Session, project_id, vendor_id) -> bool:
    return (
        db.query(ProjectQuote.id)
        .filter(ProjectQuote.project_id == project_id)
        .filter(ProjectQuote.vendor_id == vendor_id)
        .filter(ProjectQuote.is_active.is_(True))
        .filter(
            ProjectQuote.status.in_(
                (
                    ProjectQuoteStatus.submitted.value,
                    ProjectQuoteStatus.under_review.value,
                    ProjectQuoteStatus.approved.value,
                )
            )
        )
        .first()
        is not None
    )


def _recalculate(invoice: VendorPurchaseInvoice) -> None:
    subtotal = sum(
        (_money(item.amount) for item in invoice.line_items if item.is_active),
        Decimal("0.00"),
    )
    rate = Decimal(str(invoice.tax_rate_percent or "0"))
    tax_total = _money(subtotal * rate / Decimal("100"))
    invoice.subtotal = _money(subtotal)
    invoice.tax_total = tax_total
    invoice.total = _money(subtotal + tax_total)


def serialize(invoice: VendorPurchaseInvoice) -> dict:
    attachment = invoice.attachment
    return {
        "id": invoice.id,
        "project_id": invoice.project_id,
        "vendor_id": invoice.vendor_id,
        "invoice_number": invoice.invoice_number,
        "status": invoice.status,
        "currency": invoice.currency,
        "tax_rate_percent": invoice.tax_rate_percent,
        "subtotal": invoice.subtotal,
        "tax_total": invoice.tax_total,
        "total": invoice.total,
        "submitted_at": invoice.submitted_at,
        "reviewed_at": invoice.reviewed_at,
        "reviewed_by_system_user_id": invoice.reviewed_by_system_user_id,
        "review_notes": invoice.review_notes,
        "created_by_system_user_id": invoice.created_by_system_user_id,
        "attachment_stored_file_id": invoice.attachment_stored_file_id,
        "attachment_file_name": attachment.original_filename if attachment else None,
        "attachment_content_type": attachment.content_type if attachment else None,
        "attachment_file_size": attachment.file_size if attachment else None,
        "erp_purchase_order_id": invoice.erp_purchase_order_id,
        "erp_purchase_invoice_id": invoice.erp_purchase_invoice_id,
        "erp_purchase_invoice_status": invoice.erp_purchase_invoice_status,
        "erp_sync_error": invoice.erp_sync_error,
        "erp_synced_at": invoice.erp_synced_at,
        "erp_attachment_synced_at": invoice.erp_attachment_synced_at,
        "is_active": invoice.is_active,
        "created_at": invoice.created_at,
        "updated_at": invoice.updated_at,
        "line_items": [item for item in invoice.line_items if item.is_active],
    }


class VendorPurchaseInvoices:
    @staticmethod
    def for_project(db: Session, project_id: str, *, vendor_id: str) -> dict | None:
        rows = VendorPurchaseInvoices.list(
            db,
            vendor_id=vendor_id,
            project_id=project_id,
            limit=1,
            offset=0,
        )
        return rows[0] if rows else None

    @staticmethod
    def list(
        db: Session,
        *,
        vendor_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        query = _query(db).filter(VendorPurchaseInvoice.is_active.is_(True))
        if vendor_id:
            query = query.filter(
                VendorPurchaseInvoice.vendor_id == coerce_uuid(vendor_id)
            )
        if project_id:
            query = query.filter(
                VendorPurchaseInvoice.project_id == coerce_uuid(project_id)
            )
        if status:
            query = query.filter(VendorPurchaseInvoice.status == status.strip())
        rows = apply_pagination(
            query.order_by(VendorPurchaseInvoice.created_at.desc()), limit, offset
        ).all()
        return [serialize(row) for row in rows]

    @staticmethod
    def get(db: Session, invoice_id: str, *, vendor_id: str | None = None) -> dict:
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        return serialize(invoice)

    @staticmethod
    def create(
        db: Session,
        payload: VendorPurchaseInvoiceCreate,
        *,
        vendor_id: str,
        created_by_system_user_id: str | None,
    ) -> dict:
        project = _project_for_vendor(db, str(payload.project_id), vendor_id)
        existing = (
            _query(db)
            .filter(VendorPurchaseInvoice.project_id == project.id)
            .filter(VendorPurchaseInvoice.vendor_id == coerce_uuid(vendor_id))
            .filter(VendorPurchaseInvoice.is_active.is_(True))
            .one_or_none()
        )
        if existing is not None:
            return serialize(existing)
        invoice = VendorPurchaseInvoice(
            project_id=project.id,
            vendor_id=coerce_uuid(vendor_id),
            invoice_number=(payload.invoice_number or "").strip() or None,
            currency=payload.currency.upper(),
            tax_rate_percent=payload.tax_rate_percent,
            created_by_system_user_id=(
                coerce_uuid(created_by_system_user_id)
                if created_by_system_user_id
                else None
            ),
        )
        db.add(invoice)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Invoice number is already used for this vendor",
            ) from exc
        return VendorPurchaseInvoices.get(db, str(invoice.id), vendor_id=vendor_id)

    @staticmethod
    def update(
        db: Session,
        invoice_id: str,
        payload: VendorPurchaseInvoiceUpdate,
        *,
        vendor_id: str,
    ) -> dict:
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        _assert_editable(invoice)
        data = payload.model_dump(exclude_unset=True)
        if "invoice_number" in data:
            data["invoice_number"] = (data["invoice_number"] or "").strip() or None
        if data.get("currency"):
            data["currency"] = str(data["currency"]).upper()
        for key, value in data.items():
            setattr(invoice, key, value)
        _recalculate(invoice)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Invoice number is already used for this vendor",
            ) from exc
        return VendorPurchaseInvoices.get(db, invoice_id, vendor_id=vendor_id)

    @staticmethod
    def add_line(
        db: Session,
        invoice_id: str,
        payload: VendorPurchaseInvoiceLineCreate,
        *,
        vendor_id: str,
    ) -> dict:
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        _assert_editable(invoice)
        line = VendorPurchaseInvoiceLineItem(
            invoice_id=invoice.id,
            item_type=(payload.item_type or "").strip() or None,
            description=payload.description.strip(),
            quantity=payload.quantity,
            unit_price=_money(payload.unit_price),
            amount=_money(payload.quantity * payload.unit_price),
            notes=(payload.notes or "").strip() or None,
            is_active=True,
        )
        invoice.line_items.append(line)
        _recalculate(invoice)
        db.commit()
        return VendorPurchaseInvoices.get(db, invoice_id, vendor_id=vendor_id)

    @staticmethod
    def update_line(
        db: Session,
        invoice_id: str,
        line_id: str,
        payload: VendorPurchaseInvoiceLineUpdate,
        *,
        vendor_id: str,
    ) -> dict:
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        _assert_editable(invoice)
        line = next(
            (
                row
                for row in invoice.line_items
                if str(row.id) == str(line_id) and row.is_active
            ),
            None,
        )
        if line is None:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(line, key, value.strip() if isinstance(value, str) else value)
        line.amount = _money(line.quantity * line.unit_price)
        _recalculate(invoice)
        db.commit()
        return VendorPurchaseInvoices.get(db, invoice_id, vendor_id=vendor_id)

    @staticmethod
    def delete_line(
        db: Session, invoice_id: str, line_id: str, *, vendor_id: str
    ) -> dict:
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        _assert_editable(invoice)
        line = next(
            (
                row
                for row in invoice.line_items
                if str(row.id) == str(line_id) and row.is_active
            ),
            None,
        )
        if line is None:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        line.is_active = False
        _recalculate(invoice)
        db.commit()
        return VendorPurchaseInvoices.get(db, invoice_id, vendor_id=vendor_id)

    @staticmethod
    def upload_attachment(
        db: Session,
        invoice_id: str,
        *,
        vendor_id: str,
        file_name: str,
        content_type: str | None,
        content: bytes,
    ) -> dict:
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        _assert_editable(invoice)
        if not content:
            raise HTTPException(status_code=422, detail="Attachment is empty")
        old_file = invoice.attachment
        try:
            stored = file_uploads.upload(
                db=db,
                domain="attachments",
                entity_type="vendor_purchase_invoice",
                entity_id=str(invoice.id),
                original_filename=file_name or "invoice.pdf",
                content_type=content_type,
                data=content,
                uploaded_by=None,
                owner_subscriber_id=None,
            )
        except FileValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        invoice.attachment_stored_file_id = stored.id
        db.commit()
        if old_file and old_file.id != stored.id:
            file_uploads.soft_delete(db=db, file=old_file)
        return VendorPurchaseInvoices.get(db, invoice_id, vendor_id=vendor_id)

    @staticmethod
    def submit(db: Session, invoice_id: str, *, vendor_id: str) -> dict:
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        _assert_editable(invoice)
        if not (invoice.invoice_number or "").strip():
            raise HTTPException(status_code=422, detail="Invoice number is required")
        if not _has_submitted_quote(db, invoice.project_id, invoice.vendor_id):
            raise HTTPException(
                status_code=409,
                detail="A submitted vendor quote is required before invoicing",
            )
        if not any(item.is_active for item in invoice.line_items):
            raise HTTPException(
                status_code=422, detail="At least one active invoice line is required"
            )
        _recalculate(invoice)
        invoice.status = VendorPurchaseInvoiceStatus.submitted.value
        invoice.submitted_at = datetime.now(UTC)
        invoice.review_notes = None
        db.commit()
        return VendorPurchaseInvoices.get(db, invoice_id, vendor_id=vendor_id)

    @staticmethod
    def approve(
        db: Session,
        invoice_id: str,
        *,
        reviewer_system_user_id: str,
        review_notes: str | None,
    ) -> dict:
        invoice = _get(db, invoice_id)
        if invoice.status not in _REVIEWABLE:
            raise HTTPException(
                status_code=409, detail="Only submitted invoices can be approved"
            )
        _recalculate(invoice)
        invoice.status = VendorPurchaseInvoiceStatus.approved.value
        invoice.reviewed_at = datetime.now(UTC)
        invoice.reviewed_by_system_user_id = coerce_uuid(reviewer_system_user_id)
        invoice.review_notes = (review_notes or "").strip() or None
        invoice.erp_purchase_order_id = (
            invoice.project.erp_purchase_order_id or invoice.erp_purchase_order_id
        )
        db.commit()
        try:
            from app.services.dotmac_erp.purchase_invoice_sync import (
                enqueue_purchase_invoice,
            )

            enqueue_purchase_invoice(db, invoice)
            db.commit()
        except Exception as exc:  # enqueue failure is repairable by the sweeper
            db.rollback()
            invoice = _get(db, invoice_id)
            invoice.erp_sync_error = str(exc)[:500]
            db.commit()
        return VendorPurchaseInvoices.get(db, invoice_id)

    @staticmethod
    def reject(
        db: Session,
        invoice_id: str,
        *,
        reviewer_system_user_id: str,
        review_notes: str | None,
    ) -> dict:
        invoice = _get(db, invoice_id)
        if invoice.status not in _REVIEWABLE:
            raise HTTPException(
                status_code=409, detail="Only submitted invoices can be rejected"
            )
        invoice.status = VendorPurchaseInvoiceStatus.revision_requested.value
        invoice.reviewed_at = datetime.now(UTC)
        invoice.reviewed_by_system_user_id = coerce_uuid(reviewer_system_user_id)
        invoice.review_notes = (review_notes or "").strip() or None
        db.commit()
        return VendorPurchaseInvoices.get(db, invoice_id)

    @staticmethod
    def attachment_file(db: Session, invoice_id: str, *, vendor_id: str | None):
        invoice = _get(db, invoice_id)
        _assert_vendor(invoice, vendor_id)
        file = invoice.attachment
        if file is None or file.is_deleted:
            raise HTTPException(status_code=404, detail="Attachment not found")
        return file, file_uploads.stream_file(file)


vendor_purchase_invoices = VendorPurchaseInvoices()
