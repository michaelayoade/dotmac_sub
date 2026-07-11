"""Tests for the incremental-sync ``updated_since`` watermark on the billing
list endpoints (invoices / payments / credit-notes).

Context: the ERP AR sync re-listed every row each cycle via OFFSET pagination
over an unindexed global ``created_at`` sort, starving dotmac_sub's DB pool.
The fix threads an ``updated_since`` cutoff into the list query (filtering on
``updated_at``) so ERP can pull only the delta. These tests pin the filter
semantics, backward compatibility when the param is omitted, and stable
ordering for deterministic forward paging.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import CreditNote, Payment
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_invoice(db, account_id, updated_at):
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=account_id,
            currency="NGN",
            subtotal=Decimal("0.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("0.00"),
            balance_due=Decimal("0.00"),
        ),
    )
    invoice.updated_at = updated_at
    db.add(invoice)
    db.flush()
    return invoice


def test_invoice_updated_since_filters_at_or_after_cutoff(
    db_session, subscriber_account
):
    old = _make_invoice(db_session, subscriber_account.id, _T0)
    on_cutoff = _make_invoice(
        db_session, subscriber_account.id, _T0 + timedelta(days=1)
    )
    newer = _make_invoice(db_session, subscriber_account.id, _T0 + timedelta(days=2))

    cutoff = _T0 + timedelta(days=1)
    rows = billing_service.invoices.list(
        db_session,
        None,
        None,
        None,
        "updated_at",
        "asc",
        50,
        0,
        updated_since=cutoff,
    )
    ids = {r.id for r in rows}

    # Row exactly at the cutoff is inclusive; strictly-older row excluded.
    assert on_cutoff.id in ids
    assert newer.id in ids
    assert old.id not in ids


def test_invoice_updated_since_omitted_is_unchanged(db_session, subscriber_account):
    _make_invoice(db_session, subscriber_account.id, _T0)
    _make_invoice(db_session, subscriber_account.id, _T0 + timedelta(days=5))

    # Omitting the watermark returns everything (fully backward-compatible).
    rows = billing_service.invoices.list(
        db_session, None, None, None, "created_at", "desc", 50, 0
    )
    assert len(rows) == 2


def test_invoice_updated_since_ordering_is_stable(db_session, subscriber_account):
    # Two rows share the same updated_at → the id tiebreaker must give a
    # deterministic forward-paging order.
    same = _T0 + timedelta(days=3)
    a = _make_invoice(db_session, subscriber_account.id, same)
    b = _make_invoice(db_session, subscriber_account.id, same)
    later = _make_invoice(db_session, subscriber_account.id, same + timedelta(hours=1))

    rows = billing_service.invoices.list(
        db_session,
        None,
        None,
        None,
        "updated_at",
        "asc",
        50,
        0,
        updated_since=_T0,
    )
    ordered_ids = [r.id for r in rows]

    # updated_at ascending, then id ascending among the tied pair.
    tied_expected = sorted([a.id, b.id], key=str)
    assert ordered_ids[:2] == tied_expected
    assert ordered_ids[2] == later.id


def test_payment_updated_since_filters(db_session, subscriber_account):
    old = Payment(
        account_id=subscriber_account.id,
        amount=Decimal("10.00"),
        currency="NGN",
        updated_at=_T0,
    )
    new = Payment(
        account_id=subscriber_account.id,
        amount=Decimal("20.00"),
        currency="NGN",
        updated_at=_T0 + timedelta(days=2),
    )
    db_session.add_all([old, new])
    db_session.flush()

    rows = billing_service.payments.list(
        db_session,
        None,
        None,
        None,
        None,
        "updated_at",
        "asc",
        50,
        0,
        updated_since=_T0 + timedelta(days=1),
    )
    ids = {r.id for r in rows}
    assert new.id in ids
    assert old.id not in ids


def test_credit_note_updated_since_filters(db_session, subscriber_account):
    old = CreditNote(
        account_id=subscriber_account.id,
        currency="NGN",
        updated_at=_T0,
    )
    new = CreditNote(
        account_id=subscriber_account.id,
        currency="NGN",
        updated_at=_T0 + timedelta(days=2),
    )
    db_session.add_all([old, new])
    db_session.flush()

    rows = billing_service.credit_notes.list(
        db_session,
        None,
        None,
        None,
        None,
        "updated_at",
        "asc",
        50,
        0,
        updated_since=_T0 + timedelta(days=1),
    )
    ids = {r.id for r in rows}
    assert new.id in ids
    assert old.id not in ids
