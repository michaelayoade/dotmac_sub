"""Ledger entry management service.

Ledger entries are immutable financial records. Once created, they cannot
be modified. To correct an entry, create a reversing entry using the
``reverse()`` method.
"""

import logging

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

logger = logging.getLogger(__name__)

_REVERSE_TYPE = {
    LedgerEntryType.debit: LedgerEntryType.credit,
    LedgerEntryType.credit: LedgerEntryType.debit,
}


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
    def update(db: Session, entry_id: str, payload: LedgerEntryUpdate):
        entry = get_by_id(db, LedgerEntry, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        updates = payload.model_dump(exclude_unset=True)
        for field, value in updates.items():
            setattr(entry, field, value)
        db.commit()
        db.refresh(entry)
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
    def reverse(db: Session, entry_id: str, memo: str | None = None) -> LedgerEntry:
        """Create a reversing entry for an existing ledger entry.

        This is the correct way to 'undo' a ledger entry — ledger entries
        are immutable and must never be modified directly.

        Args:
            db: Database session.
            entry_id: ID of the entry to reverse.
            memo: Optional memo for the reversing entry.

        Returns:
            The newly created reversing LedgerEntry.
        """
        original = get_by_id(db, LedgerEntry, entry_id)
        if not original:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        if not original.is_active:
            raise HTTPException(
                status_code=400, detail="Ledger entry is already inactive"
            )
        reversed_type = _REVERSE_TYPE[original.entry_type]
        reversal = LedgerEntry(
            account_id=original.account_id,
            invoice_id=original.invoice_id,
            payment_id=original.payment_id,
            entry_type=reversed_type,
            source=original.source,
            amount=original.amount,
            currency=original.currency,
            memo=memo or f"Reversal of ledger entry {entry_id}",
        )
        db.add(reversal)
        original.is_active = False
        db.commit()
        db.refresh(reversal)
        logger.info("Reversed ledger entry %s with new entry %s", entry_id, reversal.id)
        return reversal

    @staticmethod
    def delete(db: Session, entry_id: str):
        entry = get_by_id(db, LedgerEntry, entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        entry.is_active = False
        db.commit()
