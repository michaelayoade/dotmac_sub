"""Invoice settlement state-machine guards (review #A3/#A6).

- A void invoice must never be resurrected to paid (recalc + allocation).
- Manual status transitions must follow the allow-list.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice, InvoiceStatus
from app.schemas.billing import InvoiceUpdate
from app.services import billing as billing_service
from app.services.billing._common import (
    _assert_invoice_allocatable,
    _recalculate_invoice_totals,
    assert_legal_invoice_transition,
)


def _invoice(db, subscriber, status, *, total="100.00", balance="0.00"):
    inv = Invoice(
        account_id=subscriber.id,
        invoice_number=f"INV-{status.value}-{subscriber.id.hex[:6]}",
        status=status,
        total=Decimal(total),
        balance_due=Decimal(balance),
        metadata_={},
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_transition_table_blocks_resurrection():
    # void is a terminal sink
    for to in (InvoiceStatus.paid, InvoiceStatus.issued, InvoiceStatus.draft):
        with pytest.raises(HTTPException) as e:
            assert_legal_invoice_transition(InvoiceStatus.void, to)
        assert e.value.status_code == 409
    # paid and terminal financial transitions belong to named owners
    with pytest.raises(HTTPException):
        assert_legal_invoice_transition(InvoiceStatus.paid, InvoiceStatus.issued)
    # legal edges pass
    assert_legal_invoice_transition(InvoiceStatus.draft, InvoiceStatus.issued)
    with pytest.raises(HTTPException):
        assert_legal_invoice_transition(InvoiceStatus.issued, InvoiceStatus.paid)
    with pytest.raises(HTTPException):
        assert_legal_invoice_transition(InvoiceStatus.paid, InvoiceStatus.void)
    assert_legal_invoice_transition(InvoiceStatus.paid, InvoiceStatus.paid)  # no-op


def test_recalc_does_not_resurrect_void(db_session, subscriber):
    """A void invoice (balance 0) must stay void through a recalc, not flip
    to paid."""
    inv = _invoice(db_session, subscriber, InvoiceStatus.void, balance="0.00")
    _recalculate_invoice_totals(db_session, inv)
    db_session.flush()
    assert inv.status == InvoiceStatus.void


def test_recalc_does_not_advance_draft(db_session, subscriber):
    inv = _invoice(db_session, subscriber, InvoiceStatus.draft, balance="0.00")
    _recalculate_invoice_totals(db_session, inv)
    db_session.flush()
    assert inv.status == InvoiceStatus.draft


def test_allocatable_guard_rejects_void_and_draft(db_session, subscriber):
    void_inv = _invoice(db_session, subscriber, InvoiceStatus.void)
    draft_inv = _invoice(db_session, subscriber, InvoiceStatus.draft)
    issued_inv = _invoice(
        db_session, subscriber, InvoiceStatus.issued, balance="100.00"
    )
    for bad in (void_inv, draft_inv):
        with pytest.raises(HTTPException) as e:
            _assert_invoice_allocatable(bad)
        assert e.value.status_code == 400
    _assert_invoice_allocatable(issued_inv)  # does not raise


def test_update_rejects_void_to_issued(db_session, subscriber):
    inv = _invoice(db_session, subscriber, InvoiceStatus.void)
    with pytest.raises(HTTPException) as e:
        billing_service.invoices.update(
            db_session, str(inv.id), InvoiceUpdate(status=InvoiceStatus.issued)
        )
    assert e.value.status_code == 409
