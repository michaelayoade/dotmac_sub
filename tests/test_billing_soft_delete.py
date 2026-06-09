"""Soft-deleted invoices (is_active=False) must not count in billing dashboard
KPIs or AR aging, even when their status is still 'issued'/'overdue'."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.services.billing.reporting import billing_reporting


def _invoice(
    db_session, account_id, balance, *, is_active=True, status=InvoiceStatus.issued
):
    inv = Invoice(
        account_id=account_id,
        invoice_number=f"INV-{uuid.uuid4()}",
        status=status,
        subtotal=Decimal(balance),
        tax_total=Decimal("0.00"),
        total=Decimal(balance),
        balance_due=Decimal(balance),
        issued_at=datetime.now(UTC) - timedelta(days=10),
        due_at=datetime.now(UTC) - timedelta(days=3),
        is_active=is_active,
    )
    db_session.add(inv)
    db_session.commit()
    return inv


def test_overview_stats_exclude_soft_deleted_invoices(db_session, subscriber):
    _invoice(db_session, subscriber.id, "100.00")  # live
    _invoice(db_session, subscriber.id, "999.00", is_active=False)  # soft-deleted

    stats = billing_reporting.get_overview_stats(db_session)
    assert stats["pending_amount"] == 100.0  # not 1099
    assert stats["total_invoices"] == 1  # soft-deleted not counted


def test_ar_aging_excludes_soft_deleted_invoices(db_session, subscriber):
    _invoice(db_session, subscriber.id, "100.00")
    _invoice(db_session, subscriber.id, "999.00", is_active=False)

    aging = billing_reporting.get_ar_aging_buckets(db_session)
    total = sum(float(v) for v in aging["totals"].values())
    assert total == 100.0
