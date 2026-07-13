"""Ledger entry management service.

Ledger entries are immutable financial records. Once created, they cannot
be modified. To correct an entry, create a reversing entry using the
``reverse()`` method.
"""

import logging

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.schemas.billing import LedgerEntryCreate, LedgerEntryUpdate
from app.services.billing._common import _validate_ledger_linkages
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    get_by_id,
    validate_enum,
)
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)

_REVERSE_TYPE = {
    LedgerEntryType.debit: LedgerEntryType.credit,
    LedgerEntryType.credit: LedgerEntryType.debit,
}

# Every reversal memo carries this reference to the entry it reverses, so the
# pairing survives an operator-supplied memo and stays machine-findable.
_REVERSAL_REFERENCE = "Reversal of ledger entry {entry_id}"


def _reversal_target_statement(entry_id: str):
    """Select the entry under a row lock for reversal serialization.

    The existing-reversal lookup is only an effective idempotency guard when
    two requests cannot both pass it before either commits. Locking the
    original entry makes the check-and-post sequence serial on PostgreSQL.
    """
    return (
        select(LedgerEntry)
        .where(LedgerEntry.id == coerce_uuid(entry_id))
        .with_for_update()
    )


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
        """Rejected. A posted ledger entry is immutable.

        This used to ``setattr`` any field of the payload onto a posted entry,
        including ``amount``, ``entry_type`` and ``is_active`` — silently
        rewriting money that had already been counted, with no audit trail.
        """
        raise HTTPException(
            status_code=409,
            detail=(
                "Ledger entries are immutable. Post a reversing entry with "
                "POST /ledger-entries/{entry_id}/reverse instead."
            ),
        )

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
        stmt = select(LedgerEntry)
        if account_id:
            stmt = stmt.where(LedgerEntry.account_id == account_id)
        if entry_type:
            stmt = stmt.where(
                LedgerEntry.entry_type
                == validate_enum(entry_type, LedgerEntryType, "entry_type")
            )
        if source:
            stmt = stmt.where(
                LedgerEntry.source == validate_enum(source, LedgerSource, "source")
            )
        if is_active is None:
            stmt = stmt.where(LedgerEntry.is_active.is_(True))
        else:
            stmt = stmt.where(LedgerEntry.is_active == is_active)
        stmt = apply_ordering(
            stmt,
            order_by,
            order_dir,
            {
                "created_at": LedgerEntry.created_at,
                # Real-world date, falling back to the import instant for native
                # / unbackfilled rows — the canonical ledger ordering key.
                "effective_date": func.coalesce(
                    LedgerEntry.effective_date, LedgerEntry.created_at
                ),
                "amount": LedgerEntry.amount,
            },
        )
        return list(db.scalars(apply_pagination(stmt, limit, offset)).all())

    @staticmethod
    def reverse(db: Session, entry_id: str, memo: str | None = None) -> LedgerEntry:
        """Post a reversing entry. The original stays active.

        The ledger is append-only: an entry's effect is undone by posting its
        opposite, never by retracting the original.

        This previously did BOTH — it posted the reversing entry *and* set
        ``original.is_active = False``. Every balance reader filters on
        ``is_active`` (``get_account_credit_balance``, ``_common.py:78``/``:90``,
        and ``customer_financial_ledger``), so the original leaving the sum and
        the reversal entering it both subtracted: the balance moved by *twice*
        the reversed amount. Reversing a NGN10,000 credit left the customer at
        -NGN10,000 instead of 0, and enforcement suspends on that balance.

        Reversing an already-reversed entry is refused, so a double-click cannot
        post two reversals against one original.

        Args:
            db: Database session.
            entry_id: ID of the entry to reverse.
            memo: Optional memo for the reversing entry.

        Returns:
            The newly created reversing LedgerEntry.
        """
        # Serialize the entire check-and-post sequence on the original entry.
        # Without this lock, two concurrent requests can both observe no
        # reversal and each post one before either transaction commits.
        original = db.scalar(_reversal_target_statement(entry_id))
        if not original:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        if not original.is_active:
            raise HTTPException(
                status_code=400, detail="Ledger entry is already inactive"
            )

        reversed_type = _REVERSE_TYPE[original.entry_type]

        # The reversal always carries a machine-findable reference to what it
        # reverses, even when the operator supplies their own wording. That
        # reference is what makes the guard below (and the D1 detector) reliable
        # instead of dependent on default memo text.
        reference = _REVERSAL_REFERENCE.format(entry_id=entry_id)
        memo_text = memo or reference
        if reference not in memo_text:
            memo_text = f"{memo_text} [{reference}]"

        # Structural check: every reversal posted since the reversal_of_entry_id
        # migration carries the link, and uq_ledger_entries_reversal_of makes a
        # second one impossible regardless of what this service does.
        already_reversed = db.scalar(
            select(LedgerEntry.id).where(
                LedgerEntry.reversal_of_entry_id == original.id
            )
        )

        # Legacy check: reversals posted BEFORE that migration have a NULL link and
        # were never backfilled (inferring the pairing from memo text could pair
        # the wrong rows, and on a ledger that corrupts money). They are still
        # findable by the memo reference, so an un-backfilled reversal continues to
        # block a re-reversal.
        if not already_reversed:
            already_reversed = db.scalar(
                select(LedgerEntry.id)
                .where(LedgerEntry.account_id == original.account_id)
                .where(LedgerEntry.entry_type == reversed_type)
                .where(LedgerEntry.reversal_of_entry_id.is_(None))
                .where(LedgerEntry.memo.ilike(f"%{reference}%"))
            )

        if already_reversed:
            raise HTTPException(
                status_code=409, detail="Ledger entry has already been reversed"
            )

        reversal = LedgerEntry(
            account_id=original.account_id,
            invoice_id=original.invoice_id,
            payment_id=original.payment_id,
            entry_type=reversed_type,
            source=original.source,
            amount=original.amount,
            currency=original.currency,
            memo=memo_text,
            reversal_of_entry_id=original.id,
        )
        db.add(reversal)
        db.commit()
        db.refresh(reversal)
        logger.info("Reversed ledger entry %s with new entry %s", entry_id, reversal.id)
        return reversal

    @staticmethod
    def delete(db: Session, entry_id: str):
        """Rejected. A posted ledger entry cannot be erased.

        This used to flip ``is_active`` with no compensating entry, silently
        moving the account's balance with no record of why.
        """
        raise HTTPException(
            status_code=409,
            detail=(
                "Ledger entries cannot be deleted. Post a reversing entry with "
                "POST /ledger-entries/{entry_id}/reverse instead."
            ),
        )
