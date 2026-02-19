"""Invoice and invoice line management services."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    TaxApplication,
)
from app.models.domain_settings import SettingDomain
from app.schemas.billing import (
    InvoiceBulkVoidRequest,
    InvoiceBulkWriteOffRequest,
    InvoiceCreate,
    InvoiceLineCreate,
    InvoiceLineUpdate,
    InvoiceUpdate,
)
from app.services import numbering, settings_spec
from app.services.billing._common import (
    _recalculate_invoice_totals,
    _resolve_tax_rate,
    _validate_account,
    _validate_invoice_line_amount,
    _validate_invoice_totals,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    get_by_id,
    validate_enum,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.response import ListResponseMixin


class Invoices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: InvoiceCreate):
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_invoice_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, InvoiceStatus, "status"
                )
        if not data.get("invoice_number"):
            generated = numbering.generate_number(
                db,
                SettingDomain.billing,
                "invoice_number",
                "invoice_number_enabled",
                "invoice_number_prefix",
                "invoice_number_padding",
                "invoice_number_start",
            )
            if generated:
                data["invoice_number"] = generated
        _validate_invoice_totals(data)
        invoice = Invoice(**data)
        db.add(invoice)
        db.commit()
        db.refresh(invoice)

        # Emit invoice.created event
        emit_event(
            db,
            EventType.invoice_created,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "total": str(invoice.total) if invoice.total else None,
                "currency": invoice.currency,
                "status": invoice.status.value if invoice.status else None,
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )

        return invoice

    @staticmethod
    def get(db: Session, invoice_id: str):
        invoice = get_by_id(
            db,
            Invoice,
            invoice_id,
            options=[
                selectinload(Invoice.lines),
                selectinload(Invoice.payment_allocations),
            ],
        )
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return invoice

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(Invoice).options(
            selectinload(Invoice.lines),
            selectinload(Invoice.payment_allocations),
        )
        if account_id:
            query = query.filter(Invoice.account_id == account_id)
        if status:
            query = query.filter(
                Invoice.status == validate_enum(status, InvoiceStatus, "status")
            )
        if is_active is None:
            query = query.filter(Invoice.is_active.is_(True))
        else:
            query = query.filter(Invoice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Invoice.created_at,
                "due_at": Invoice.due_at,
                "issued_at": Invoice.issued_at,
                "status": Invoice.status,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, invoice_id: str, payload: InvoiceUpdate):
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        previous_status = invoice.status
        data = payload.model_dump(exclude_unset=True)
        if "account_id" in data:
            _validate_account(db, str(data["account_id"]))
        if "currency" in data and data["currency"] != invoice.currency:
            raise HTTPException(status_code=400, detail="Currency does not match invoice")
        merged = {
            "subtotal": data.get("subtotal", invoice.subtotal),
            "tax_total": data.get("tax_total", invoice.tax_total),
            "total": data.get("total", invoice.total),
            "balance_due": data.get("balance_due", invoice.balance_due),
        }
        _validate_invoice_totals(merged)
        for key, value in data.items():
            setattr(invoice, key, value)
        db.commit()
        db.refresh(invoice)

        # Emit invoice events based on status transitions
        new_status = invoice.status
        if previous_status != new_status:
            payload_dict = {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "total": str(invoice.total) if invoice.total else None,
                "currency": invoice.currency,
                "from_status": previous_status.value if previous_status else None,
                "to_status": new_status.value if new_status else None,
            }

            if new_status == InvoiceStatus.issued:
                emit_event(
                    db,
                    EventType.invoice_sent,
                    payload_dict,
                    account_id=invoice.account_id,
                    invoice_id=invoice.id,
                )
            elif new_status == InvoiceStatus.paid:
                emit_event(
                    db,
                    EventType.invoice_paid,
                    payload_dict,
                    account_id=invoice.account_id,
                    invoice_id=invoice.id,
                )
            elif new_status == InvoiceStatus.overdue:
                emit_event(
                    db,
                    EventType.invoice_overdue,
                    payload_dict,
                    account_id=invoice.account_id,
                    invoice_id=invoice.id,
                )

        return invoice

    @staticmethod
    def delete(db: Session, invoice_id: str):
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        invoice.is_active = False
        db.commit()

    @staticmethod
    def write_off(db: Session, invoice_id: str, memo: str | None = None):
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.balance_due <= 0:
            raise HTTPException(status_code=400, detail="Invoice has no balance due")
        entry = LedgerEntry(
            account_id=invoice.account_id,
            invoice_id=invoice.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=invoice.balance_due,
            currency=invoice.currency,
            memo=memo or "Write-off",
        )
        db.add(entry)
        invoice.balance_due = Decimal("0.00")
        invoice.status = InvoiceStatus.void
        db.commit()
        db.refresh(invoice)
        return invoice

    @staticmethod
    def void(db: Session, invoice_id: str, memo: str | None = None):
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        invoice.status = InvoiceStatus.void
        invoice.balance_due = Decimal("0.00")
        if memo:
            invoice.memo = memo
        db.commit()
        db.refresh(invoice)
        return invoice

    @staticmethod
    def bulk_write_off(db: Session, payload: InvoiceBulkWriteOffRequest) -> int:
        if not payload.invoice_ids:
            raise HTTPException(status_code=400, detail="invoice_ids required")
        ids = [coerce_uuid(invoice_id) for invoice_id in payload.invoice_ids]
        invoices = db.query(Invoice).filter(Invoice.id.in_(ids)).all()
        if len(invoices) != len(ids):
            raise HTTPException(status_code=404, detail="One or more invoices not found")
        updated = 0
        for invoice in invoices:
            if invoice.balance_due <= 0:
                continue
            entry = LedgerEntry(
                account_id=invoice.account_id,
                invoice_id=invoice.id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.adjustment,
                amount=invoice.balance_due,
                currency=invoice.currency,
                memo=payload.memo or "Write-off",
            )
            db.add(entry)
            invoice.balance_due = Decimal("0.00")
            invoice.status = InvoiceStatus.void
            updated += 1
        db.commit()
        return updated

    @staticmethod
    def bulk_write_off_response(db: Session, payload: InvoiceBulkWriteOffRequest) -> dict:
        updated = Invoices.bulk_write_off(db, payload)
        return {"updated": updated}

    @staticmethod
    def bulk_void(db: Session, payload: InvoiceBulkVoidRequest) -> int:
        if not payload.invoice_ids:
            raise HTTPException(status_code=400, detail="invoice_ids required")
        ids = [coerce_uuid(invoice_id) for invoice_id in payload.invoice_ids]
        invoices = db.query(Invoice).filter(Invoice.id.in_(ids)).all()
        if len(invoices) != len(ids):
            raise HTTPException(status_code=404, detail="One or more invoices not found")
        for invoice in invoices:
            invoice.status = InvoiceStatus.void
            invoice.balance_due = Decimal("0.00")
            if payload.memo:
                invoice.memo = payload.memo
        db.commit()
        return len(invoices)

    @staticmethod
    def bulk_void_response(db: Session, payload: InvoiceBulkVoidRequest) -> dict:
        updated = Invoices.bulk_void(db, payload)
        return {"updated": updated}


class InvoiceLines(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: InvoiceLineCreate):
        invoice = get_by_id(db, Invoice, payload.invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        _resolve_tax_rate(db, str(payload.tax_rate_id) if payload.tax_rate_id else None)
        data = payload.model_dump(exclude={"amount"})
        fields_set = payload.model_fields_set
        if "tax_application" not in fields_set:
            default_tax_application = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_tax_application"
            )
            if default_tax_application:
                data["tax_application"] = validate_enum(
                    default_tax_application, TaxApplication, "tax_application"
                )
        amount = _validate_invoice_line_amount(
            payload.quantity, payload.unit_price, payload.amount
        )
        line = InvoiceLine(**data, amount=amount)
        try:
            db.add(line)
            db.flush()
            _recalculate_invoice_totals(db, invoice)
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def get(db: Session, line_id: str):
        line = get_by_id(db, InvoiceLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        return line

    @staticmethod
    def list(
        db: Session,
        invoice_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(InvoiceLine)
        if invoice_id:
            query = query.filter(InvoiceLine.invoice_id == invoice_id)
        if is_active is None:
            query = query.filter(InvoiceLine.is_active.is_(True))
        else:
            query = query.filter(InvoiceLine.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": InvoiceLine.created_at, "amount": InvoiceLine.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, line_id: str, payload: InvoiceLineUpdate):
        line = get_by_id(db, InvoiceLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        data = payload.model_dump(exclude_unset=True)
        if "tax_rate_id" in data:
            _resolve_tax_rate(db, str(data["tax_rate_id"]) if data["tax_rate_id"] else None)
        quantity = data.get("quantity", line.quantity)
        unit_price = data.get("unit_price", line.unit_price)
        amount = data.get("amount")
        data["amount"] = _validate_invoice_line_amount(quantity, unit_price, amount)
        for key, value in data.items():
            setattr(line, key, value)
        invoice = get_by_id(db, Invoice, line.invoice_id)
        try:
            if invoice:
                db.flush()
                _recalculate_invoice_totals(db, invoice)
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def delete(db: Session, line_id: str):
        line = get_by_id(db, InvoiceLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Invoice line not found")
        line.is_active = False
        invoice = get_by_id(db, Invoice, line.invoice_id)
        if invoice:
            db.flush()
            _recalculate_invoice_totals(db, invoice)
        db.commit()
