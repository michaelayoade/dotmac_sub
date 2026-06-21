"""Bulk invoice actions money-correctness (review #A5).

- bulk_mark_paid records a real payment so the 'paid' status survives a recalc
  (the raw status poke silently reverted).
- bulk_void (service) skips paid/void invoices instead of stranding payments.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.services import billing as billing_service
from app.services.billing._common import _recalculate_invoice_totals
from app.services.web_billing_invoice_bulk import bulk_mark_paid


def _issued(db, subscriber, num):
    inv = Invoice(
        account_id=subscriber.id,
        invoice_number=num,
        status=InvoiceStatus.issued,
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        currency="NGN",
        metadata_={},
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_bulk_mark_paid_survives_recalc(db_session, subscriber):
    inv = _issued(db_session, subscriber, "INV-BULK-1")
    updated = bulk_mark_paid(db_session, str(inv.id))
    assert updated == [str(inv.id)]

    db_session.refresh(inv)
    assert inv.status == InvoiceStatus.paid
    assert inv.balance_due == Decimal("0.00")

    # The key fix: a later recalc must NOT revert it (a real allocation backs it).
    _recalculate_invoice_totals(db_session, inv)
    db_session.flush()
    assert inv.status == InvoiceStatus.paid


def test_bulk_void_service_skips_paid_invoice(db_session, subscriber):
    from app.schemas.billing import InvoiceBulkVoidRequest

    paid = _issued(db_session, subscriber, "INV-BULK-PAID")
    paid.status = InvoiceStatus.paid
    paid.balance_due = Decimal("0.00")
    issued = _issued(db_session, subscriber, "INV-BULK-ISSUED")
    db_session.commit()

    count = billing_service.invoices.bulk_void(
        db_session,
        InvoiceBulkVoidRequest(invoice_ids=[str(paid.id), str(issued.id)]),
    )
    db_session.refresh(paid)
    db_session.refresh(issued)
    assert count == 1  # only the issued one voided
    assert paid.status == InvoiceStatus.paid  # paid invoice NOT voided
    assert issued.status == InvoiceStatus.void
