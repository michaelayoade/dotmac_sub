"""Tests for the phantom opening-balance debit reconciliation one-off.

These lock in the CURRENT production-validated behavior of the script
(₦208.9M / 2,230 rows applied with --scope non-terminal). They must not be
changed to drive a logic change; the script logic is frozen.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.billing import (
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from scripts.one_off.reconcile_phantom_opening_debits import (
    OPENING_MEMO,
    REVERSAL_MEMO,
    _apply,
    _classify,
    _existing_reversal_amounts,
    _load_candidates,
)


def _subscriber(
    db,
    *,
    status: SubscriberStatus = SubscriberStatus.active,
    deposit: Decimal | None = Decimal("0.00"),
) -> Subscriber:
    sub = Subscriber(
        first_name="P",
        last_name="Q",
        email=f"{uuid.uuid4()}@example.com",
        status=status,
        deposit=deposit,
    )
    db.add(sub)
    db.flush()
    return sub


def _opening_debit(
    db,
    sub: Subscriber,
    *,
    amount: Decimal = Decimal("1000.00"),
    memo: str = OPENING_MEMO,
    entry_type: LedgerEntryType = LedgerEntryType.debit,
) -> LedgerEntry:
    entry = LedgerEntry(
        account_id=sub.id,
        entry_type=entry_type,
        source=LedgerSource.adjustment,
        category=LedgerCategory.deposit,
        amount=amount,
        currency="NGN",
        memo=memo,
    )
    db.add(entry)
    db.flush()
    return entry


def _candidate_for(candidates, entry_id):
    matches = [c for c in candidates if c.entry.id == entry_id]
    assert len(matches) == 1
    return matches[0]


def test_genuine_phantom_debit_is_eligible_and_credit_equals_debit(db_session):
    sub = _subscriber(db_session, deposit=Decimal("0.00"))
    entry = _opening_debit(db_session, sub, amount=Decimal("1500.00"))
    db_session.flush()

    candidates = _load_candidates(db_session, "active")
    cand = _candidate_for(candidates, entry.id)

    assert cand.classification == "eligible_phantom_debit"
    assert cand.eligible is True
    assert cand.existing_reversal == Decimal("0.00")
    # compensating credit (remaining) equals the original debit amount
    assert cand.remaining == Decimal("1500.00")


def test_positive_deposit_phantom_debit_is_eligible(db_session):
    sub = _subscriber(db_session, deposit=Decimal("250.00"))
    entry = _opening_debit(db_session, sub, amount=Decimal("999.99"))
    db_session.flush()

    cand = _candidate_for(_load_candidates(db_session, "active"), entry.id)
    assert cand.eligible is True
    assert cand.remaining == Decimal("999.99")


def test_already_compensated_debit_is_idempotent(db_session):
    sub = _subscriber(db_session, deposit=Decimal("0.00"))
    entry = _opening_debit(db_session, sub, amount=Decimal("1000.00"))
    # Prior reversal credit already exists for the full debit amount.
    db_session.add(
        LedgerEntry(
            account_id=sub.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            category=LedgerCategory.deposit,
            amount=Decimal("1000.00"),
            currency="NGN",
            memo=REVERSAL_MEMO.format(id=entry.id),
        )
    )
    db_session.flush()

    reversals = _existing_reversal_amounts(db_session, [entry.id])
    assert reversals[str(entry.id)] == Decimal("1000.00")

    cand = _candidate_for(_load_candidates(db_session, "active"), entry.id)
    assert cand.existing_reversal == Decimal("1000.00")
    assert cand.remaining <= Decimal("0.00")
    assert cand.classification == "already_compensated"
    assert cand.eligible is False

    # And _apply must NOT write a second compensating credit.
    before = db_session.query(LedgerEntry).count()
    written = _apply(db_session, [cand])
    assert written == 0
    assert db_session.query(LedgerEntry).count() == before


def test_nonmatching_rows_not_classified_phantom(db_session):
    # Different memo.
    sub_a = _subscriber(db_session, deposit=Decimal("0.00"))
    other_memo = _opening_debit(db_session, sub_a, memo="Some unrelated ledger memo")
    # Negative deposit (legit arrears).
    sub_b = _subscriber(db_session, deposit=Decimal("-500.00"))
    neg_deposit = _opening_debit(db_session, sub_b)
    # Wrong entry_type (credit, not debit).
    sub_c = _subscriber(db_session, deposit=Decimal("0.00"))
    wrong_type = _opening_debit(db_session, sub_c, entry_type=LedgerEntryType.credit)
    db_session.flush()

    candidates = _load_candidates(db_session, "active")
    candidate_ids = {c.entry.id for c in candidates}

    # memo and entry_type filters happen in the query: those rows never load.
    assert other_memo.id not in candidate_ids
    assert wrong_type.id not in candidate_ids

    # negative-deposit row loads but is classified non-phantom / ineligible.
    neg = _candidate_for(candidates, neg_deposit.id)
    assert neg.eligible is False
    assert neg.classification == "legit_negative_deposit"


def test_classify_pure_unit_behavior(db_session):
    sub = _subscriber(db_session, deposit=Decimal("0.00"))
    scope = {SubscriberStatus.active}

    # Already compensated wins first (remaining <= 0).
    assert _classify(sub, Decimal("0.00"), Decimal("0.00"), scope) == (
        "already_compensated",
        False,
    )
    # Null deposit -> review.
    assert _classify(sub, None, Decimal("10.00"), scope) == (
        "deposit_null_review",
        False,
    )
    # Negative deposit -> legit arrears.
    assert _classify(sub, Decimal("-1.00"), Decimal("10.00"), scope) == (
        "legit_negative_deposit",
        False,
    )
    # In-scope, non-negative deposit -> eligible.
    assert _classify(sub, Decimal("0.00"), Decimal("10.00"), scope) == (
        "eligible_phantom_debit",
        True,
    )


def test_dry_run_writes_nothing(db_session):
    sub = _subscriber(db_session, deposit=Decimal("0.00"))
    _opening_debit(db_session, sub, amount=Decimal("1234.56"))
    db_session.flush()

    before = db_session.query(LedgerEntry).count()
    # Dry-run path: load + classify only, never call _apply.
    candidates = _load_candidates(db_session, "active")
    assert any(c.eligible for c in candidates)
    assert db_session.query(LedgerEntry).count() == before


def test_apply_writes_compensating_credit_for_eligible(db_session):
    sub = _subscriber(db_session, deposit=Decimal("0.00"))
    entry = _opening_debit(db_session, sub, amount=Decimal("2000.00"))
    db_session.flush()

    cand = _candidate_for(_load_candidates(db_session, "active"), entry.id)
    written = _apply(db_session, [cand])
    assert written == 1

    credit = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.memo == REVERSAL_MEMO.format(id=entry.id))
        .one()
    )
    assert credit.entry_type == LedgerEntryType.credit
    assert credit.source == LedgerSource.adjustment
    assert credit.category == LedgerCategory.deposit
    assert credit.amount == Decimal("2000.00")
    assert credit.account_id == entry.account_id
