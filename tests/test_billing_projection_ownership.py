from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.services.billing.reporting import BillingReporting
from app.services.customer_portal_context import get_outstanding_balance


def _invoice(
    account_id,
    *,
    number: str,
    status: InvoiceStatus,
    total: str,
    balance: str,
    **kwargs,
):
    return Invoice(
        account_id=account_id,
        invoice_number=number,
        status=status,
        currency="NGN",
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(balance),
        issued_at=datetime.now(UTC) - timedelta(days=60),
        due_at=datetime.now(UTC) - timedelta(days=30),
        **kwargs,
    )


def test_portal_balance_uses_complete_canonical_projection_when_list_is_capped(
    db_session, subscriber_account
):
    db_session.add_all(
        [
            _invoice(
                subscriber_account.id,
                number=f"PORTAL-{index}",
                status=InvoiceStatus.overdue,
                total="100.00",
                balance="100.00",
            )
            for index in range(51)
        ]
    )
    db_session.commit()

    result = get_outstanding_balance(db_session, str(subscriber_account.id))

    assert len(result["invoices"]) == 50
    assert result["invoices_truncated"] is True
    assert result["outstanding_balance"] == Decimal("5100.00")


def test_reporting_derives_settled_value_and_debt_from_money_not_status(
    db_session, subscriber_account
):
    subscriber_account.min_balance = Decimal("9999.00")
    db_session.add(
        _invoice(
            subscriber_account.id,
            number="PAID-WITH-BALANCE",
            status=InvoiceStatus.paid,
            total="100.00",
            balance="40.00",
        )
    )
    db_session.add(
        _invoice(
            subscriber_account.id,
            number="OPEN-AR",
            status=InvoiceStatus.overdue,
            total="25.00",
            balance="25.00",
        )
    )
    db_session.commit()

    overview = BillingReporting.get_overview_stats(db_session)
    account_stats = BillingReporting.get_account_stats(db_session)

    assert overview["total_revenue"] == 60.0
    assert overview["paid_count"] == 0
    assert overview["overdue_amount"] == 25.0
    assert account_stats["total_balance"] == 25.0
    assert account_stats["total_balance_currency"] == "NGN"


def test_ar_aging_excludes_proforma_rows(db_session, subscriber_account):
    collectible = _invoice(
        subscriber_account.id,
        number="AR-REAL",
        status=InvoiceStatus.overdue,
        total="80.00",
        balance="80.00",
    )
    proforma = _invoice(
        subscriber_account.id,
        number="PF-QUOTE",
        status=InvoiceStatus.overdue,
        total="500.00",
        balance="500.00",
        is_proforma=True,
    )
    db_session.add_all([collectible, proforma])
    db_session.commit()

    aging = BillingReporting.get_ar_aging_buckets(db_session)
    rows = [invoice for bucket in aging["buckets"].values() for invoice in bucket]

    assert [invoice.id for invoice in rows] == [collectible.id]
    assert sum(aging["totals"].values()) == 80.0
