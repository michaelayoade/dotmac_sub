"""Invoice recompute must not leave a stale 'paid' status.

Regression: _recalculate_invoice_totals had no branch for the
"fully unpaid again" case (balance_due > 0 with no succeeded payment / credit).
When a payment was refunded, balance_due rose back to the total but the status
stayed 'paid'. Now it reverts to issued/overdue.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.services.billing._common import _recalculate_invoice_totals


def _make_invoice(db, subscriber, *, number, due_at=None):
    invoice = Invoice(
        account_id=subscriber.id,
        invoice_number=number,
        status=InvoiceStatus.paid,  # stale "paid"...
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("0.00"),  # ...with no actual succeeded payment
        paid_at=datetime.now(UTC),
        due_at=due_at,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def test_recalc_reverts_unpaid_paid_invoice_to_issued(db_session, subscriber):
    invoice = _make_invoice(db_session, subscriber, number="INV-RECALC-1")

    _recalculate_invoice_totals(db_session, invoice)
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.balance_due == Decimal("100.00")
    assert invoice.status == InvoiceStatus.issued
    assert invoice.paid_at is None


def test_recalc_reverts_to_overdue_when_past_due(db_session, subscriber):
    past = datetime.now(UTC) - timedelta(days=5)
    invoice = _make_invoice(db_session, subscriber, number="INV-RECALC-2", due_at=past)

    _recalculate_invoice_totals(db_session, invoice)
    db_session.commit()
    db_session.refresh(invoice)

    assert invoice.status == InvoiceStatus.overdue
