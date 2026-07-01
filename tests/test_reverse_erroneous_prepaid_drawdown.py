"""Tests for the erroneous prepaid drawdown reversal one-off.

Covers the current behavior of the dry-run/apply script that writes append-only
compensating CREDIT ledger entries to reverse erroneous "Prepaid charge: 30d"
DEBIT rows for monthly-cycle prepaid plans.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from scripts.one_off.reverse_erroneous_prepaid_drawdown import (
    REVERSAL_MEMO_PREFIX,
    _base_query,
    _classify_entry,
    _reversal_memo,
    _subscription_id_from_memo,
    main,
)

# Window covering the debits created in these tests (script defaults span this).
FROM_DATE = "2026-06-16"
TO_DATE = "2026-06-22"
IN_WINDOW = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subscriber(db) -> Subscriber:
    s = Subscriber(
        first_name="Test",
        last_name="User",
        email=f"{uuid.uuid4().hex}@example.com",
        status=SubscriberStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db.add(s)
    db.flush()
    return s


def _offer(db, *, billing_cycle: BillingCycle = BillingCycle.monthly) -> CatalogOffer:
    offer = CatalogOffer(
        name="Prepaid Monthly",
        code=f"OFF-{uuid.uuid4().hex[:8]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=billing_cycle,
        billing_mode=BillingMode.prepaid,
    )
    db.add(offer)
    db.flush()
    return offer


def _subscription(db, subscriber, offer) -> Subscription:
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
    )
    db.add(sub)
    db.flush()
    return sub


def _debit(
    db,
    *,
    account_id,
    subscription_id,
    amount=Decimal("9500.00"),
    invoice_id=None,
    created_at=IN_WINDOW,
    memo_suffix="",
) -> LedgerEntry:
    memo = f"Prepaid charge: 30d (sub={subscription_id}){memo_suffix}"
    entry = LedgerEntry(
        account_id=account_id,
        invoice_id=invoice_id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.adjustment,
        amount=amount,
        currency="NGN",
        memo=memo,
        created_at=created_at,
        effective_date=created_at,
    )
    db.add(entry)
    db.flush()
    return entry


# ---------------------------------------------------------------------------
# _subscription_id_from_memo
# ---------------------------------------------------------------------------


def test_subscription_id_from_memo_extracts_uuid():
    sub_id = uuid.uuid4()
    memo = f"Prepaid charge: 30d (sub={sub_id})"
    assert _subscription_id_from_memo(memo) == sub_id


def test_subscription_id_from_memo_non_matching():
    assert _subscription_id_from_memo("Prepaid charge: 30d (no id here)") is None
    assert _subscription_id_from_memo("") is None
    assert _subscription_id_from_memo(None) is None


# ---------------------------------------------------------------------------
# _classify_entry
# ---------------------------------------------------------------------------


def test_classify_eligible(db_session):
    sub_acct = _subscriber(db_session)
    offer = _offer(db_session)
    sub = _subscription(db_session, sub_acct, offer)
    entry = _debit(db_session, account_id=sub_acct.id, subscription_id=sub.id)
    db_session.flush()

    classification, subscription, reversal_memo = _classify_entry(db_session, entry)
    assert classification == "eligible"
    assert subscription is not None and subscription.id == sub.id
    assert reversal_memo == _reversal_memo(entry.id)


def test_classify_already_reversed(db_session):
    sub_acct = _subscriber(db_session)
    offer = _offer(db_session)
    sub = _subscription(db_session, sub_acct, offer)
    entry = _debit(db_session, account_id=sub_acct.id, subscription_id=sub.id)
    db_session.flush()

    # Pre-existing compensating credit for this exact debit.
    db_session.add(
        LedgerEntry(
            account_id=sub_acct.id,
            invoice_id=None,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=entry.amount,
            currency="NGN",
            memo=_reversal_memo(entry.id),
        )
    )
    db_session.flush()

    classification, _sub_obj, _memo = _classify_entry(db_session, entry)
    assert classification == "already_reversed"


def test_classify_out_of_scope_non_monthly(db_session):
    sub_acct = _subscriber(db_session)
    offer = _offer(db_session, billing_cycle=BillingCycle.daily)
    sub = _subscription(db_session, sub_acct, offer)
    entry = _debit(db_session, account_id=sub_acct.id, subscription_id=sub.id)
    db_session.flush()

    classification, _sub_obj, _memo = _classify_entry(db_session, entry)
    assert classification == "skip_non_monthly_offer"


def test_classify_out_of_scope_no_subscription_id(db_session):
    sub_acct = _subscriber(db_session)
    entry = LedgerEntry(
        account_id=sub_acct.id,
        invoice_id=None,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.adjustment,
        amount=Decimal("9500.00"),
        currency="NGN",
        memo="Prepaid charge: 30d (no subscription)",
        created_at=IN_WINDOW,
    )
    db_session.add(entry)
    db_session.flush()

    classification, _sub_obj, _memo = _classify_entry(db_session, entry)
    assert classification == "skip_no_subscription_id"


# ---------------------------------------------------------------------------
# Fix A: invoice_id IS NULL safety filter on the base query
# ---------------------------------------------------------------------------


def test_base_query_excludes_non_null_invoice_id(db_session):
    sub_acct = _subscriber(db_session)
    offer = _offer(db_session)
    sub = _subscription(db_session, sub_acct, offer)

    inv = Invoice(
        account_id=sub_acct.id,
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        status=InvoiceStatus.issued,
        total=Decimal("9500.00"),
        balance_due=Decimal("9500.00"),
        is_active=True,
    )
    db_session.add(inv)
    db_session.flush()

    null_debit = _debit(db_session, account_id=sub_acct.id, subscription_id=sub.id)
    invoiced_debit = _debit(
        db_session,
        account_id=sub_acct.id,
        subscription_id=sub.id,
        invoice_id=inv.id,
    )
    db_session.flush()

    start_at = datetime(2026, 6, 16, tzinfo=UTC)
    end_at = datetime(2026, 6, 23, tzinfo=UTC)
    matched = _base_query(db_session, start_at, end_at, lock_rows=False).all()
    matched_ids = {e.id for e in matched}
    assert null_debit.id in matched_ids
    assert invoiced_debit.id not in matched_ids


# ---------------------------------------------------------------------------
# main(): dry-run vs apply, sign/amount, idempotency
# ---------------------------------------------------------------------------


def _run_main(db_session, monkeypatch, tmp_path, *, apply: bool):
    import scripts.one_off.reverse_erroneous_prepaid_drawdown as mod

    # main() opens its own session via SessionLocal() and closes it; bind it to
    # the test session and keep that session alive for post-run assertions.
    monkeypatch.setattr(mod, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(db_session, "commit", db_session.flush)

    argv = [
        "prog",
        "--from-date",
        FROM_DATE,
        "--to-date",
        TO_DATE,
        "--output",
        str(tmp_path / "out.csv"),
    ]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr("sys.argv", argv)
    main()


def _credits_for(db_session, account_id):
    return (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.account_id == account_id)
        .filter(LedgerEntry.entry_type == LedgerEntryType.credit)
        .filter(LedgerEntry.memo.ilike(f"{REVERSAL_MEMO_PREFIX}%"))
        .all()
    )


def test_dry_run_writes_nothing(db_session, monkeypatch, tmp_path):
    sub_acct = _subscriber(db_session)
    offer = _offer(db_session)
    sub = _subscription(db_session, sub_acct, offer)
    _debit(db_session, account_id=sub_acct.id, subscription_id=sub.id)
    db_session.flush()

    _run_main(db_session, monkeypatch, tmp_path, apply=False)

    assert _credits_for(db_session, sub_acct.id) == []


def test_apply_writes_compensating_credit(db_session, monkeypatch, tmp_path):
    sub_acct = _subscriber(db_session)
    offer = _offer(db_session)
    sub = _subscription(db_session, sub_acct, offer)
    amount = Decimal("9500.00")
    debit = _debit(
        db_session, account_id=sub_acct.id, subscription_id=sub.id, amount=amount
    )
    db_session.flush()

    _run_main(db_session, monkeypatch, tmp_path, apply=True)

    credits = _credits_for(db_session, sub_acct.id)
    assert len(credits) == 1
    credit = credits[0]
    assert credit.entry_type == LedgerEntryType.credit
    assert credit.amount == amount
    assert credit.invoice_id is None
    assert credit.memo == _reversal_memo(debit.id)


def test_apply_is_idempotent(db_session, monkeypatch, tmp_path):
    sub_acct = _subscriber(db_session)
    offer = _offer(db_session)
    sub = _subscription(db_session, sub_acct, offer)
    _debit(db_session, account_id=sub_acct.id, subscription_id=sub.id)
    db_session.flush()

    _run_main(db_session, monkeypatch, tmp_path, apply=True)
    assert len(_credits_for(db_session, sub_acct.id)) == 1

    # A second apply must not double-credit: the prior credit makes the debit
    # classify as already_reversed.
    _run_main(db_session, monkeypatch, tmp_path, apply=True)
    assert len(_credits_for(db_session, sub_acct.id)) == 1
