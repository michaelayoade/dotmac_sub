"""Credit note management services."""

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
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
from app.schemas.billing import (
    CreditNoteApplyRequest,
    CreditNoteCreate,
    CreditNoteLineCreate,
    CreditNoteLineUpdate,
    CreditNoteUpdate,
)
from app.services import numbering, settings_spec
from app.services.billing._common import (
    _recalculate_credit_note_totals,
    _recalculate_invoice_totals,
    _resolve_tax_rate,
    _validate_account,
    _validate_credit_note_totals,
    _validate_invoice_line_amount,
    get_account_credit_balance,
    lock_account,
)
from app.services.billing.ledger import LedgerEntries
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    get_by_id,
    round_money,
    to_decimal,
    validate_enum,
)
from app.services.locking import lock_for_update
from app.services.response import ListResponseMixin
from app.services.sync_feeds import apply_sync_page, sync_page_response

logger = logging.getLogger(__name__)


def _issuance_entry(db: Session, credit_note_id) -> LedgerEntry | None:
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.credit_note_id == credit_note_id)
        .filter(LedgerEntry.credit_note_application_id.is_(None))
        .filter(LedgerEntry.reversal_of_entry_id.is_(None))
        .one_or_none()
    )


def _post_issuance(db: Session, credit_note: CreditNote) -> LedgerEntry:
    existing = _issuance_entry(db, credit_note.id)
    if existing is not None:
        return existing
    amount = round_money(to_decimal(credit_note.total))
    if amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="An issued credit note must have a total greater than 0",
        )
    issued_at = credit_note.issued_at or datetime.now(UTC)
    credit_note.issued_at = issued_at
    reference = credit_note.credit_number or str(credit_note.id)
    entry = LedgerEntry(
        account_id=credit_note.account_id,
        credit_note_id=credit_note.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.credit_note,
        amount=amount,
        currency=credit_note.currency,
        memo=f"Credit note {reference} issued",
        effective_date=issued_at,
    )
    db.add(entry)
    db.flush()
    return entry


def _finish(db: Session, entity, *, commit: bool):
    if commit:
        db.commit()
        db.refresh(entity)
    else:
        db.flush()
    return entity


def _require_draft(credit_note: CreditNote) -> None:
    if credit_note.status != CreditNoteStatus.draft:
        raise HTTPException(
            status_code=409,
            detail="Issued credit notes are immutable; void and reissue instead",
        )


class CreditNotes(ListResponseMixin):
    @staticmethod
    def create(
        db: Session,
        payload: CreditNoteCreate,
        *,
        commit: bool = True,
    ):
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        requested_status = data.get("status", CreditNoteStatus.draft)
        if requested_status not in {CreditNoteStatus.draft, CreditNoteStatus.issued}:
            raise HTTPException(
                status_code=400,
                detail="Credit notes must be created as draft or issued",
            )
        invoice = None
        if payload.invoice_id:
            invoice = get_by_id(db, Invoice, payload.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if invoice.account_id != payload.account_id:
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to account"
                )
            if "currency" not in fields_set:
                data["currency"] = invoice.currency
            elif data["currency"] != invoice.currency:
                raise HTTPException(
                    status_code=400, detail="Currency does not match invoice"
                )
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
        if requested_status == CreditNoteStatus.issued:
            data["issued_at"] = datetime.now(UTC)
            lock_account(db, str(payload.account_id))
        _validate_credit_note_totals(data)
        if (
            requested_status == CreditNoteStatus.issued
            and round_money(to_decimal(data["total"])) <= 0
        ):
            raise HTTPException(
                status_code=400,
                detail="An issued credit note must have a total greater than 0",
            )
        credit_note = CreditNote(**data)
        try:
            db.add(credit_note)
            db.flush()
            if requested_status == CreditNoteStatus.issued:
                _post_issuance(db, credit_note)
            return _finish(db, credit_note, commit=commit)
        except SQLAlchemyError:
            if commit:
                db.rollback()
            raise

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
        *,
        updated_since: datetime | None = None,
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
                CreditNote.status == validate_enum(status, CreditNoteStatus, "status")
            )
        if is_active is None:
            query = query.filter(CreditNote.is_active.is_(True))
        else:
            query = query.filter(CreditNote.is_active == is_active)
        # Incremental-sync watermark (see Invoices.list); backed by
        # ix_credit_notes_is_active_updated_at.
        if updated_since is not None:
            query = query.filter(CreditNote.updated_at >= updated_since)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": CreditNote.created_at,
                "updated_at": CreditNote.updated_at,
                "status": CreditNote.status,
            },
        )
        # Stable, keyset-friendly tiebreaker for deterministic forward paging.
        query = query.order_by(CreditNote.id.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_sync(
        db: Session,
        *,
        account_id: str | None,
        status: str | None,
        is_active: bool | None,
        updated_since: datetime | None,
        limit: int,
        offset: int,
    ):
        query = db.query(CreditNote).options(
            selectinload(
                CreditNote.lines.and_(CreditNoteLine.is_active.is_(True))
            ).selectinload(CreditNoteLine.tax_rate)
        )
        if account_id:
            query = query.filter(CreditNote.account_id == account_id)
        if status:
            query = query.filter(
                CreditNote.status == validate_enum(status, CreditNoteStatus, "status")
            )
        if is_active is None:
            query = query.filter(CreditNote.is_active.is_(True))
        else:
            query = query.filter(CreditNote.is_active == is_active)
        return apply_sync_page(
            query,
            CreditNote,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ).all()

    @classmethod
    def sync_list_response(cls, db: Session, **kwargs):
        items = cls.list_for_sync(db, **kwargs)
        return sync_page_response(items, limit=kwargs["limit"], offset=kwargs["offset"])

    @staticmethod
    def update(
        db: Session,
        credit_note_id: str,
        payload: CreditNoteUpdate,
        *,
        commit: bool = True,
    ):
        current = get_by_id(db, CreditNote, credit_note_id)
        if not current:
            raise HTTPException(status_code=404, detail="Credit note not found")
        lock_account(db, str(current.account_id))
        credit_note = lock_for_update(db, CreditNote, coerce_uuid(credit_note_id))
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        data = payload.model_dump(exclude_unset=True)
        if "account_id" in data:
            raise HTTPException(
                status_code=400, detail="Cannot change credit note account"
            )
        requested_status = data.pop("status", None)
        if credit_note.status != CreditNoteStatus.draft:
            if data:
                raise HTTPException(
                    status_code=409,
                    detail="Issued credit notes are immutable; void and reissue instead",
                )
            if requested_status not in {None, credit_note.status}:
                raise HTTPException(
                    status_code=409,
                    detail="Use the credit-note apply or void operation",
                )
        elif requested_status not in {
            None,
            CreditNoteStatus.draft,
            CreditNoteStatus.issued,
        }:
            raise HTTPException(
                status_code=409,
                detail="A draft credit note can only be issued by this operation",
            )
        prospective_invoice_id = data.get("invoice_id", credit_note.invoice_id)
        if prospective_invoice_id:
            invoice = get_by_id(db, Invoice, prospective_invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if invoice.account_id != credit_note.account_id:
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to account"
                )
            if data.get("currency", credit_note.currency) != invoice.currency:
                raise HTTPException(
                    status_code=400, detail="Currency does not match invoice"
                )
        merged = {
            "subtotal": data.get("subtotal", credit_note.subtotal),
            "tax_total": data.get("tax_total", credit_note.tax_total),
            "total": data.get("total", credit_note.total),
            "applied_total": credit_note.applied_total,
        }
        _validate_credit_note_totals(merged)
        if (
            requested_status == CreditNoteStatus.issued
            and round_money(to_decimal(merged["total"])) <= 0
        ):
            raise HTTPException(
                status_code=400,
                detail="An issued credit note must have a total greater than 0",
            )
        try:
            for key, value in data.items():
                setattr(credit_note, key, value)
            if requested_status == CreditNoteStatus.issued:
                credit_note.status = CreditNoteStatus.issued
                if credit_note.issued_at is None:
                    credit_note.issued_at = datetime.now(UTC)
                db.flush()
                _post_issuance(db, credit_note)
            return _finish(db, credit_note, commit=commit)
        except SQLAlchemyError:
            if commit:
                db.rollback()
            raise

    @staticmethod
    def issue(
        db: Session,
        credit_note_id: str,
        *,
        commit: bool = True,
    ) -> CreditNote:
        return CreditNotes.update(
            db,
            credit_note_id,
            CreditNoteUpdate(status=CreditNoteStatus.issued),
            commit=commit,
        )

    @staticmethod
    def delete(db: Session, credit_note_id: str):
        credit_note = get_by_id(db, CreditNote, credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        _require_draft(credit_note)
        credit_note.is_active = False
        db.commit()

    @staticmethod
    def void(db: Session, credit_note_id: str):
        current = get_by_id(db, CreditNote, credit_note_id)
        if not current:
            raise HTTPException(status_code=404, detail="Credit note not found")
        lock_account(db, str(current.account_id))
        credit_note = lock_for_update(db, CreditNote, coerce_uuid(credit_note_id))
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.status == CreditNoteStatus.void:
            raise HTTPException(status_code=400, detail="Credit note already void")
        if credit_note.applied_total > 0:
            raise HTTPException(
                status_code=400, detail="Credit note has applied balance"
            )
        try:
            if credit_note.status != CreditNoteStatus.draft:
                issuance = _issuance_entry(db, credit_note.id)
                if issuance is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Credit note has no issuance posting; reconcile it "
                            "before voiding"
                        ),
                    )
                LedgerEntries.reverse(
                    db,
                    str(issuance.id),
                    memo=(
                        f"Credit note {credit_note.credit_number or credit_note.id} "
                        "voided"
                    ),
                    commit=False,
                )
            credit_note.status = CreditNoteStatus.void
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        db.refresh(credit_note)
        return credit_note

    @staticmethod
    def apply(db: Session, credit_note_id: str, payload: CreditNoteApplyRequest):
        # Lock the credit note before the check-then-act below: `remaining` is
        # derived from `applied_total`, and two concurrent applies would each
        # read a snapshot missing the other's uncommitted application — both
        # passing the balance check and over-applying the note (a $100 note
        # spent twice). The row lock serializes applies per note so the later
        # one sees the committed applied_total and is rejected. Lock order is
        # Account -> CreditNote -> Invoice, matching every wallet spender.
        current = get_by_id(db, CreditNote, credit_note_id)
        if not current:
            raise HTTPException(status_code=404, detail="Credit note not found")
        lock_account(db, str(current.account_id))
        credit_note = lock_for_update(db, CreditNote, coerce_uuid(credit_note_id))
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        if credit_note.status in {CreditNoteStatus.draft, CreditNoteStatus.void}:
            raise HTTPException(status_code=400, detail="Credit note is not applicable")
        issuance = _issuance_entry(db, credit_note.id)
        if issuance is None:
            raise HTTPException(
                status_code=409,
                detail="Credit note has no issuance posting; reconcile it before applying",
            )
        if (
            db.query(LedgerEntry.id)
            .filter(LedgerEntry.reversal_of_entry_id == issuance.id)
            .first()
            is not None
        ):
            raise HTTPException(
                status_code=409, detail="Credit note issuance has been reversed"
            )
        invoice = lock_for_update(db, Invoice, coerce_uuid(payload.invoice_id))
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.account_id != credit_note.account_id:
            raise HTTPException(
                status_code=400, detail="Invoice does not belong to account"
            )
        if credit_note.invoice_id and credit_note.invoice_id != invoice.id:
            raise HTTPException(
                status_code=400, detail="Credit note is tied to another invoice"
            )
        if invoice.currency != credit_note.currency:
            raise HTTPException(
                status_code=400, detail="Currency does not match invoice"
            )
        remaining = round_money(credit_note.total - credit_note.applied_total)
        if remaining <= 0:
            raise HTTPException(
                status_code=400, detail="Credit note has no available balance"
            )
        if invoice.balance_due <= 0:
            raise HTTPException(status_code=400, detail="Invoice has no balance due")
        available_credit = get_account_credit_balance(
            db, str(credit_note.account_id), currency=credit_note.currency
        )
        if available_credit <= 0:
            raise HTTPException(
                status_code=400, detail="Account has no spendable credit"
            )
        amount = payload.amount or min(remaining, invoice.balance_due, available_credit)
        amount = round_money(to_decimal(amount))
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        if amount > remaining:
            raise HTTPException(
                status_code=400, detail="Amount exceeds credit note balance"
            )
        if amount > invoice.balance_due:
            raise HTTPException(
                status_code=400, detail="Amount exceeds invoice balance"
            )
        if amount > available_credit:
            raise HTTPException(
                status_code=400, detail="Amount exceeds spendable account credit"
            )
        application = CreditNoteApplication(
            credit_note_id=credit_note.id,
            invoice_id=invoice.id,
            amount=amount,
            memo=payload.memo,
        )
        try:
            db.add(application)
            db.flush()
            db.add_all(
                [
                    LedgerEntry(
                        account_id=invoice.account_id,
                        credit_note_id=credit_note.id,
                        credit_note_application_id=application.id,
                        entry_type=LedgerEntryType.debit,
                        source=LedgerSource.credit_note,
                        amount=amount,
                        currency=invoice.currency,
                        memo=payload.memo or "Credit note allocated to invoice",
                    ),
                    LedgerEntry(
                        account_id=invoice.account_id,
                        invoice_id=invoice.id,
                        credit_note_id=credit_note.id,
                        credit_note_application_id=application.id,
                        entry_type=LedgerEntryType.credit,
                        source=LedgerSource.credit_note,
                        amount=amount,
                        currency=invoice.currency,
                        memo=payload.memo or "Credit note applied",
                    ),
                ]
            )
            db.flush()
            _recalculate_invoice_totals(db, invoice)
            _recalculate_credit_note_totals(db, credit_note)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        db.refresh(application)
        return application


class CreditNoteLines(ListResponseMixin):
    @staticmethod
    def create(
        db: Session,
        payload: CreditNoteLineCreate,
        *,
        commit: bool = True,
    ):
        credit_note = get_by_id(db, CreditNote, payload.credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        _require_draft(credit_note)
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
            if commit:
                db.commit()
        except SQLAlchemyError:
            if commit:
                db.rollback()
            raise
        if commit:
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
        credit_note = get_by_id(db, CreditNote, line.credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        _require_draft(credit_note)
        data = payload.model_dump(exclude_unset=True)
        if "credit_note_id" in data:
            raise HTTPException(status_code=400, detail="Cannot change credit note")
        if "tax_rate_id" in data:
            _resolve_tax_rate(
                db, str(data["tax_rate_id"]) if data["tax_rate_id"] else None
            )
        quantity = data.get("quantity", line.quantity)
        unit_price = data.get("unit_price", line.unit_price)
        amount = data.get("amount")
        data["amount"] = _validate_invoice_line_amount(quantity, unit_price, amount)
        for key, value in data.items():
            setattr(line, key, value)
        try:
            if credit_note:
                db.flush()
                _recalculate_credit_note_totals(db, credit_note)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        db.refresh(line)
        return line

    @staticmethod
    def delete(db: Session, line_id: str):
        line = get_by_id(db, CreditNoteLine, line_id)
        if not line:
            raise HTTPException(status_code=404, detail="Credit note line not found")
        credit_note = get_by_id(db, CreditNote, line.credit_note_id)
        if not credit_note:
            raise HTTPException(status_code=404, detail="Credit note not found")
        _require_draft(credit_note)
        line.is_active = False
        if credit_note:
            db.flush()
            _recalculate_credit_note_totals(db, credit_note)
        db.commit()


class CreditNoteApplications(ListResponseMixin):
    @staticmethod
    def get(db: Session, application_id: str):
        application = get_by_id(db, CreditNoteApplication, application_id)
        if not application:
            raise HTTPException(
                status_code=404, detail="Credit note application not found"
            )
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
            {
                "created_at": CreditNoteApplication.created_at,
                "amount": CreditNoteApplication.amount,
            },
        )
        return apply_pagination(query, limit, offset).all()
