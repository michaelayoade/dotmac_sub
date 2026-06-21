"""Bad-debt write-off uses a dedicated `written_off` status (review #32).

written_off is closed-but-not-collected: distinct from paid (cash collected)
and void (never existed). The loss lives in the ledger; the invoice is terminal
and excluded from outstanding, never resurrected, and not voidable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice, InvoiceStatus, LedgerEntry, LedgerSource
from app.services import billing as billing_service
from app.services.billing._common import (
    ALLOWED_INVOICE_TRANSITIONS,
    _recalculate_invoice_totals,
)


def _issued(db, subscriber, num, balance="100.00"):
    inv = Invoice(
        account_id=subscriber.id,
        invoice_number=num,
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal(balance),
        currency="NGN",
        metadata_={},
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_write_off_sets_written_off_not_void_or_paid(db_session, subscriber):
    inv = _issued(db_session, subscriber, "INV-WO-1")
    result = billing_service.invoices.write_off(
        db_session, str(inv.id), memo="bad debt"
    )

    assert result.status == InvoiceStatus.written_off
    assert result.status != InvoiceStatus.paid  # not cash
    assert result.status != InvoiceStatus.void  # not "never existed"
    assert result.balance_due == Decimal("0.00")

    # The loss is recorded in the ledger as a credit adjustment (source of truth).
    adj = (
        db_session.query(LedgerEntry)
        .filter(LedgerEntry.invoice_id == inv.id)
        .filter(LedgerEntry.source == LedgerSource.adjustment)
        .all()
    )
    assert adj, "write-off must leave an adjustment ledger entry"


def test_written_off_is_terminal_not_resurrected(db_session, subscriber):
    inv = _issued(db_session, subscriber, "INV-WO-2")
    billing_service.invoices.write_off(db_session, str(inv.id))
    db_session.refresh(inv)

    _recalculate_invoice_totals(db_session, inv)
    db_session.flush()
    assert inv.status == InvoiceStatus.written_off  # not flipped to paid


def test_written_off_cannot_be_voided(db_session, subscriber):
    inv = _issued(db_session, subscriber, "INV-WO-3")
    billing_service.invoices.write_off(db_session, str(inv.id))
    with pytest.raises(HTTPException) as e:
        billing_service.invoices.void(db_session, str(inv.id))
    assert e.value.status_code == 400


def test_written_off_transition_table_is_sink_and_reachable():
    # reachable from active states
    assert (
        InvoiceStatus.written_off in ALLOWED_INVOICE_TRANSITIONS[InvoiceStatus.issued]
    )
    assert (
        InvoiceStatus.written_off in ALLOWED_INVOICE_TRANSITIONS[InvoiceStatus.overdue]
    )
    # terminal sink
    assert ALLOWED_INVOICE_TRANSITIONS[InvoiceStatus.written_off] == set()
