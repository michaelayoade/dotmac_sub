"""Tests for the account-level un-waller (paid-up-but-walled restore).

Post-cutover, available balance = local ledger credit minus open invoice AR (the
imported-deposit fallback was retired). "Owing" is therefore modelled with an
open invoice; "paid up" is no open AR (balance 0) or a covering credit.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.billing.unwall_paid_accounts import (
    find_walled_paid_account_ids,
    unwall_cohort,
)

_counter = iter(range(900000, 999999))


def _sub(db, *, status, owing: bool = False) -> Subscriber:
    n = next(_counter)
    sub = Subscriber(
        first_name="Walled",
        last_name=str(n),
        email=f"walled-{n}@example.com",
        status=status,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    if owing:
        # An open invoice makes available balance negative (debt, no credit).
        db.add(
            Invoice(
                account_id=sub.id,
                status=InvoiceStatus.issued,
                subtotal=Decimal("50.00"),
                tax_total=Decimal("0.00"),
                total=Decimal("50.00"),
                balance_due=Decimal("50.00"),
            )
        )
        db.commit()
    return sub


def test_gate_includes_paid_up_walled_excludes_owing_and_active(db_session):
    paid_blocked = _sub(db_session, status=SubscriberStatus.blocked)
    zero_suspended = _sub(db_session, status=SubscriberStatus.suspended)
    owing_blocked = _sub(db_session, status=SubscriberStatus.blocked, owing=True)
    paid_active = _sub(db_session, status=SubscriberStatus.active)

    ids = find_walled_paid_account_ids(db_session)

    assert str(paid_blocked.id) in ids  # walled + paid up (no open AR)
    assert str(zero_suspended.id) in ids  # walled + exactly zero counts as paid up
    assert str(owing_blocked.id) not in ids  # owes money (open invoice)
    assert str(paid_active.id) not in ids  # not walled


def test_targeted_mode_skips_owing_account(db_session):
    owing = _sub(db_session, status=SubscriberStatus.blocked, owing=True)
    paid = _sub(db_session, status=SubscriberStatus.blocked)

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
    sub = _sub(db_session, status=SubscriberStatus.blocked)

    unwall_cohort(db_session, account_ids=[str(sub.id)], dry_run=True)

    db_session.refresh(sub)
    assert sub.status == SubscriberStatus.blocked  # untouched by dry-run
