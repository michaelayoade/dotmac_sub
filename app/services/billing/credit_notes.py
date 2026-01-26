"""Credit note management services."""

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    CreditNote,
    CreditNoteApplication,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.domain_settings import SettingDomain
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    get_by_id,
    round_money,
    validate_enum,
)
from app.services.response import ListResponseMixin
from app.services import settings_spec
from app.services import numbering
from app.schemas.billing import (
    CreditNoteCreate,
    CreditNoteLineCreate,
    CreditNoteLineUpdate,
    CreditNoteUpdate,
    CreditNoteApplyRequest,
)
from app.services.billing._common import (
    _validate_account,
    _validate_credit_note_totals,
    _validate_invoice_line_amount,
    _resolve_tax_rate,
    _recalculate_invoice_totals,
    _recalculate_credit_note_totals,
)


class CreditNotes(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CreditNoteCreate):
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        invoice = None
        if payload.invoice_id:
            invoice = get_by_id(db, Invoice, payload.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if invoice.account_id != payload.account_id:
                raise HTTPException(status_code=400, detail="Invoice does not belong to account")
            if "currency" not in fields_set:
                data["currency"] = invoice.currency
            elif data["currency"] != invoice.currency:
                raise HTTPException(status_code=400, detail="Currency does not match invoice")
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        if not data.get("credit_number"):
            generated = numbering.generate_number(
                db,
                SettingDomain.billing,
                "credit_note_number",
                "credit_note_number_enabled",
                "credit_note_number_prefix",
                "credit_note_number_padding",
                "credit_note_number_start",
            )
            if generated:
                data["credit_number"] = generated
        _validate_credit_note_totals(data)
        credit_note = CreditNote(**data)
        db.add(credit_note)
        db.commit()
        db.refresh(credit_note)
        return credit_note

    @staticmethod
    def get(db: Session, credit_note_id: str):
        credit_note = get_by_id(
            db,
            CreditNote,
            credit_note_id,
            options=[
                selectinload(CreditNote.lines),
                selectinload(CreditNote.applications),
            ],
        )
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        return credit_note

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        invoice_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CreditNote).options(
            selectinload(CreditNote.lines),
            selectinload(CreditNote.applications),
        )
        if account_id:
            query = query.filter(CreditNote.account_id == account_id)
        if invoice_id:
            query = query.filter(CreditNote.invoice_id == coerce_uuid(invoice_id))
        if status:
            query = query.filter(
                CreditNote.status
                == validate_enum(status, CreditNoteStatus, "status")
            )
        if is_active is None:
            query = query.filter(CreditNote.is_active.is_(True))
        else:
            query = query.filter(CreditNote.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": CreditNote.created_at, "status": CreditNote.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, credit_note_id: str, payload: CreditNoteUpdate):
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        data = payload.model_dump(exclude_unset=True)
        if "account_id" in data:
            _validate_account(db, str(data["account_id"]))
        if "invoice_id" in data:
            invoice = get_by_id(db, Invoice, data["invoice_id"])
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if invoice.account_id != credit_note.account_id:
                raise HTTPException(status_code=400, detail="Invoice does not belong to account")
            if "currency" in data:
                if data["currency"] != invoice.currency:
                    raise HTTPException(status_code=400, detail="Currency does not match invoice")
            elif credit_note.currency != invoice.currency:
                raise HTTPException(status_code=400, detail="Currency does not match invoice")
        merged = {
            "subtotal": data.get("subtotal", credit_note.subtotal),
            "tax_total": data.get("tax_total", credit_note.tax_total),
            "total": data.get("total", credit_note.total),
            "applied_total": credit_note.applied_total,
        }
        _validate_credit_note_totals(merged)
        for key, value in data.items():
            setattr(credit_note, key, value)
        db.commit()
        db.refresh(credit_note)
        return credit_note

    @staticmethod
    def delete(db: Session, credit_note_id: str):
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        credit_note.is_active = False
        db.commit()

    @staticmethod
    def void(db: Session, credit_note_id: str):
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.applied_total > 0:
            raise HTTPException(status_code=400, detail="Credit note has applied balance")
        credit_note.status = CreditNoteStatus.void
        db.commit()
        db.refresh(credit_note)
        return credit_note

    @staticmethod
    def apply(db: Session, credit_note_id: str, payload: CreditNoteApplyRequest):
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.status in {CreditNoteStatus.draft, CreditNoteStatus.void}:
            raise HTTPException(status_code=400, detail="Credit note is not applicable")
        invoice = get_by_id(db, Invoice, payload.invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.account_id != credit_note.account_id:
            raise HTTPException(status_code=400, detail="Invoice does not belong to account")
        if credit_note.invoice_id and credit_note.invoice_id != invoice.id:
            raise HTTPException(status_code=400, detail="Credit note is tied to another invoice")
        if invoice.currency != credit_note.currency:
            raise HTTPException(status_code=400, detail="Currency does not match invoice")
        remaining = round_money(credit_note.total - credit_note.applied_total)
        if remaining <= 0:
            raise HTTPException(status_code=400, detail="Credit note has no available balance")
        if invoice.balance_due <= 0:
            raise HTTPException(status_code=400, detail="Invoice has no balance due")
        amount = payload.amount or min(remaining, invoice.balance_due)
        amount = round_money(Decimal(str(amount)))
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        if amount > remaining:
            raise HTTPException(status_code=400, detail="Amount exceeds credit note balance")
        if amount > invoice.balance_due:
            raise HTTPException(status_code=400, detail="Amount exceeds invoice balance")
        application = CreditNoteApplication(
            credit_note_id=credit_note.id,
            invoice_id=invoice.id,
            amount=amount,
            memo=payload.memo,
        )
        entry = LedgerEntry(
            account_id=invoice.account_id,
            invoice_id=invoice.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.credit_note,
            amount=amount,
            currency=invoice.currency,
            memo=payload.memo or "Credit note applied",
        )
        db.add(application)
        db.add(entry)
        try:
            db.flush()
            _recalculate_invoice_totals(db, invoice)
            _recalculate_credit_note_totals(db, credit_note)
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(application)
        return application


class CreditNoteLines(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CreditNoteLineCreate):
        credit_note = get_by_id(db, CreditNote, payload.credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.status == CreditNoteStatus.void:
            raise HTTPException(status_code=400, detail="Credit note is void")
        _resolve_tax_rate(db, str(payload.tax_rate_id) if payload.tax_rate_id else None)
        data = payload.model_dump(exclude={"amount"})
        amount = _validate_invoice_line_amount(
            payload.quantity, payload.unit_price, payload.amount
        )
        line = CreditNoteLine(**data, amount=amount)
        try:
            db.add(line)
            db.flush()
            _recalculate_credit_note_totals(db, credit_note)
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def get(db: Session, line_id: str):
        line = get_by_id(db, CreditNoteLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Credit note line not found")
        return line

    @staticmethod
    def list(
        db: Session,
        credit_note_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CreditNoteLine)
        if credit_note_id:
            query = query.filter(CreditNoteLine.credit_note_id == credit_note_id)
        if is_active is None:
            query = query.filter(CreditNoteLine.is_active.is_(True))
        else:
            query = query.filter(CreditNoteLine.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": CreditNoteLine.created_at, "amount": CreditNoteLine.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, line_id: str, payload: CreditNoteLineUpdate):
        line = get_by_id(db, CreditNoteLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Credit note line not found")
        data = payload.model_dump(exclude_unset=True)
        if "credit_note_id" in data:
            raise HTTPException(status_code=400, detail="Cannot change credit note")
        if "tax_rate_id" in data:
            _resolve_tax_rate(db, str(data["tax_rate_id"]) if data["tax_rate_id"] else None)
        quantity = data.get("quantity", line.quantity)
        unit_price = data.get("unit_price", line.unit_price)
        amount = data.get("amount")
        data["amount"] = _validate_invoice_line_amount(quantity, unit_price, amount)
        for key, value in data.items():
            setattr(line, key, value)
        credit_note = get_by_id(db, CreditNote, line.credit_note_id)
        try:
            if credit_note:
                db.flush()
                _recalculate_credit_note_totals(db, credit_note)
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def delete(db: Session, line_id: str):
        line = get_by_id(db, CreditNoteLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Credit note line not found")
        line.is_active = False
        credit_note = get_by_id(db, CreditNote, line.credit_note_id)
        if credit_note:
            db.flush()
            _recalculate_credit_note_totals(db, credit_note)
        db.commit()


class CreditNoteApplications(ListResponseMixin):
    @staticmethod
    def get(db: Session, application_id: str):
        application = get_by_id(db, CreditNoteApplication, application_id)
        if not application:
            raise HTTPException(status_code=404, detail="Credit note application not found")
        return application

    @staticmethod
    def list(
        db: Session,
        credit_note_id: str | None,
        invoice_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CreditNoteApplication)
        if credit_note_id:
            query = query.filter(CreditNoteApplication.credit_note_id == credit_note_id)
        if invoice_id:
            query = query.filter(CreditNoteApplication.invoice_id == invoice_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": CreditNoteApplication.created_at, "amount": CreditNoteApplication.amount},
        )
        return apply_pagination(query, limit, offset).all()
