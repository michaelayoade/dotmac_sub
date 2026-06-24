"""Tests for the account-level un-waller (paid-up-but-walled restore)."""

from __future__ import annotations

from decimal import Decimal

from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing.unwall_paid_accounts import (
    find_walled_paid_account_ids,
    unwall_cohort,
)

_counter = iter(range(900000, 999999))


def _sub(db, *, status, deposit) -> Subscriber:
    n = next(_counter)
    sub = Subscriber(
        first_name="Walled",
        last_name=str(n),
        email=f"walled-{n}@example.com",
        status=status,
        # splynx_customer_id + deposit make get_available_balance return the
        # imported deposit rather than the local ledger.
        splynx_customer_id=n,
        deposit=deposit,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def test_gate_includes_paid_up_walled_excludes_owing_and_active(db_session):
    paid_blocked = _sub(
        db_session, status=SubscriberStatus.blocked, deposit=Decimal("100.00")
    )
    zero_suspended = _sub(
        db_session, status=SubscriberStatus.suspended, deposit=Decimal("0.00")
    )
    owing_blocked = _sub(
        db_session, status=SubscriberStatus.blocked, deposit=Decimal("-50.00")
    )
    paid_active = _sub(
        db_session, status=SubscriberStatus.active, deposit=Decimal("100.00")
    )

    ids = find_walled_paid_account_ids(db_session)

    assert str(paid_blocked.id) in ids  # walled + paid up
    assert str(zero_suspended.id) in ids  # walled + exactly zero counts as paid up
    assert str(owing_blocked.id) not in ids  # owes money
    assert str(paid_active.id) not in ids  # not walled


def test_targeted_mode_skips_owing_account(db_session):
    owing = _sub(db_session, status=SubscriberStatus.blocked, deposit=Decimal("-10.00"))
    paid = _sub(db_session, status=SubscriberStatus.blocked, deposit=Decimal("5.00"))

    summary = unwall_cohort(
        db_session,
        account_ids=[str(owing.id), str(paid.id)],
        dry_run=True,
    )

    # Paid-up gate still applies in targeted mode: only the paid account is a candidate.
    candidate_ids = {r.account_id for r in summary.results}
    assert str(paid.id) in candidate_ids
    assert str(owing.id) not in candidate_ids
    assert summary.candidates == 1


def test_dry_run_writes_nothing(db_session):
    sub = _sub(db_session, status=SubscriberStatus.blocked, deposit=Decimal("100.00"))

    unwall_cohort(db_session, account_ids=[str(sub.id)], dry_run=True)

    db_session.refresh(sub)
    assert sub.status == SubscriberStatus.blocked  # untouched by dry-run
