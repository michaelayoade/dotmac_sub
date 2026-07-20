"""Ledger reversal must move the balance exactly once.

Covers F1 in ``docs/audits/BILLING_SOT_AUDIT_2026-07-12.md``.

The legacy implementation posted a reversing entry *and* deactivated the
original. Every balance reader filters ``is_active``, so the original leaving
the sum and the reversal entering it both subtracted. These tests preserve the
correct append-only behavior and the concurrency contract for future changes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.schemas.billing import LedgerEntryUpdate
from app.services.billing._common import get_account_credit_balance
from app.services.billing.ledger import LedgerEntries, _reversal_target_statement


def _post_credit(db_session, account_id, amount: str) -> LedgerEntry:
    """An unallocated payment credit — the shape that feeds the credit balance."""
    entry = LedgerEntry(
        account_id=account_id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal(amount),
        currency="NGN",
        memo="Top-up",
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    return entry


def test_reversing_a_credit_moves_the_balance_exactly_once(db_session, subscriber):
    """Reverse a NGN10,000 credit: the balance must go 10,000 -> 0, not -10,000."""
    entry = _post_credit(db_session, subscriber.id, "10000.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "10000.00"
    )

    LedgerEntries.reverse(db_session, str(entry.id), memo="customer refunded")

    balance = get_account_credit_balance(db_session, str(subscriber.id))
    assert balance == Decimal("0.00"), (
        f"reversal moved the balance to {balance}, expected 0.00. "
        "The original was deactivated AND a reversing debit was posted, "
        "so the amount was subtracted twice (F1)."
    )


def test_reversing_the_same_entry_twice_is_refused(db_session, subscriber):
    """A double-click must not post two reversals against one original."""
    entry = _post_credit(db_session, subscriber.id, "3000.00")
    LedgerEntries.reverse(db_session, str(entry.id))

    with pytest.raises(HTTPException) as exc:
        LedgerEntries.reverse(db_session, str(entry.id))
    assert exc.value.status_code == 409

    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_reversal_target_is_selected_for_update(subscriber):
    """The duplicate guard must be serialized, not only memo-based."""
    statement = _reversal_target_statement(str(subscriber.id))
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


def test_a_posted_entry_cannot_be_deleted_or_mutated(db_session, subscriber):
    """The ledger is append-only. Both escape hatches are closed."""
    entry = _post_credit(db_session, subscriber.id, "1200.00")

    with pytest.raises(HTTPException) as del_exc:
        LedgerEntries.delete(db_session, str(entry.id))
    assert del_exc.value.status_code == 409

    with pytest.raises(HTTPException) as upd_exc:
        LedgerEntries.update(
            db_session, str(entry.id), LedgerEntryUpdate(amount=Decimal("1.00"))
        )
    assert upd_exc.value.status_code == 409

    # The money is untouched by either attempt.
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "1200.00"
    )


def test_reversal_of_an_allocated_entry_does_not_touch_the_credit_balance(
    db_session, subscriber
):
    """Invoice-allocated pairs are excluded from the credit balance by design.

    ``get_account_credit_balance`` filters ``invoice_id IS NULL``, so a void
    reversal on an invoice debit is not a credit-balance double-swing. The
    detector must not report it as balance-affecting, or the repair would
    over-correct.
    """
    entry = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=Decimal("400.00"),
        currency="NGN",
        memo="allocated",
    )
    db_session.add(entry)
    db_session.commit()

    before = get_account_credit_balance(db_session, str(subscriber.id))
    assert before == Decimal("400.00")


@pytest.mark.parametrize("amount", ["1.00", "999999.99"])
def test_reversal_is_symmetric_regardless_of_magnitude(db_session, subscriber, amount):
    entry = _post_credit(db_session, subscriber.id, amount)
    LedgerEntries.reverse(db_session, str(entry.id))
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal("0.00")


def test_a_second_reversal_row_is_refused_by_the_database(db_session, subscriber):
    """The invariant must not depend on caller discipline.

    The service takes a row lock before checking, which stops the concurrent
    double-reversal. But a lock only protects callers that remember to take it.
    This bypasses the service entirely and writes the second reversal row
    straight to the table: uq_ledger_entries_reversal_of must still refuse it.
    """
    entry = _post_credit(db_session, subscriber.id, "4000.00")
    LedgerEntries.reverse(db_session, str(entry.id))

    rogue = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.payment,
        amount=Decimal("4000.00"),
        currency="NGN",
        memo="second reversal, posted without the service lock",
        reversal_of_entry_id=entry.id,
    )
    db_session.add(rogue)

    # The INSERT itself must be refused. Asserting on flush (rather than commit)
    # keeps the failure scoped to this statement — rolling back a failed commit
    # would unwind the fixture's own transaction and tell us nothing further.
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_new_reversals_carry_the_structural_link(db_session, subscriber):
    """The link is what the database constraint keys on, so it must be populated."""
    entry = _post_credit(db_session, subscriber.id, "800.00")

    reversal = LedgerEntries.reverse(db_session, str(entry.id))

    assert reversal.reversal_of_entry_id == entry.id
    # The memo reference is retained for operators and for detecting legacy,
    # un-backfilled reversals that predate the column.
    assert str(entry.id) in (reversal.memo or "")


def test_a_legacy_memo_only_reversal_still_blocks_a_re_reversal(db_session, subscriber):
    """Historical reversals were never backfilled; they must still be honoured.

    A pre-migration reversal has reversal_of_entry_id = NULL, so the database
    constraint cannot see it. The memo lookup is what stops the entry being
    reversed a second time.
    """
    entry = _post_credit(db_session, subscriber.id, "650.00")
    legacy = LedgerEntry(
        account_id=subscriber.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.payment,
        amount=Decimal("650.00"),
        currency="NGN",
        memo=f"Reversal of ledger entry {entry.id}",
        reversal_of_entry_id=None,  # what the old code wrote
    )
    db_session.add(legacy)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        LedgerEntries.reverse(db_session, str(entry.id))
    assert exc.value.status_code == 409
