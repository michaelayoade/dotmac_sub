"""Ledger entry management service."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.schemas.billing import LedgerEntryCreate, LedgerEntryUpdate
from app.services.billing._common import _validate_ledger_linkages
from app.services.common import (
    apply_ordering,
    apply_pagination,
    get_by_id,
    validate_enum,
)
from app.services.response import ListResponseMixin


class LedgerEntries(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: LedgerEntryCreate):
        _validate_ledger_linkages(
            db,
            str(payload.account_id),
            str(payload.invoice_id) if payload.invoice_id else None,
            str(payload.payment_id) if payload.payment_id else None,
        )
        entry = LedgerEntry(**payload.model_dump())
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def get(db: Session, entry_id: str):
        entry = get_by_id(db, LedgerEntry, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        return entry

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        entry_type: str | None,
        source: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(LedgerEntry)
        if account_id:
            query = query.filter(LedgerEntry.account_id == account_id)
        if entry_type:
            query = query.filter(
                LedgerEntry.entry_type
                == validate_enum(entry_type, LedgerEntryType, "entry_type")
            )
        if source:
            query = query.filter(
                LedgerEntry.source == validate_enum(source, LedgerSource, "source")
            )
        if is_active is None:
            query = query.filter(LedgerEntry.is_active.is_(True))
        else:
            query = query.filter(LedgerEntry.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": LedgerEntry.created_at, "amount": LedgerEntry.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, entry_id: str, payload: LedgerEntryUpdate):
        entry = get_by_id(db, LedgerEntry, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = str(data.get("account_id", entry.account_id))
        invoice_id = data.get("invoice_id", entry.invoice_id)
        payment_id = data.get("payment_id", entry.payment_id)
        _validate_ledger_linkages(
            db,
            account_id,
            str(invoice_id) if invoice_id else None,
            str(payment_id) if payment_id else None,
        )
        for key, value in data.items():
            setattr(entry, key, value)
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def delete(db: Session, entry_id: str):
        entry = get_by_id(db, LedgerEntry, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        entry.is_active = False
        db.commit()
