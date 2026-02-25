from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Reseller, Subscriber
from app.services.web_billing_overview import build_ar_aging_data


def _create_invoice(
    db_session,
    *,
    account_id,
    due_at: datetime,
    balance_due: str,
    status: InvoiceStatus = InvoiceStatus.issued,
):
    invoice = Invoice(
        account_id=account_id,
        invoice_number=f"INV-{datetime.now(UTC).timestamp()}-{balance_due}",
        status=status,
        subtotal=Decimal(balance_due),
        tax_total=Decimal("0.00"),
        total=Decimal(balance_due),
        balance_due=Decimal(balance_due),
        issued_at=due_at - timedelta(days=14),
        due_at=due_at,
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def test_ar_aging_includes_counts_and_supports_bucket_selection(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(db_session, account_id=subscriber.id, due_at=now + timedelta(days=2), balance_due="50.00")
    _create_invoice(db_session, account_id=subscriber.id, due_at=now - timedelta(days=10), balance_due="30.00")
    _create_invoice(db_session, account_id=subscriber.id, due_at=now - timedelta(days=95), balance_due="20.00")
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        due_at=now - timedelta(days=40),
        balance_due="15.00",
        status=InvoiceStatus.paid,
    )

    result = build_ar_aging_data(db_session, period="all", bucket="1_30")

    assert result["counts"]["current"] == 1
    assert result["counts"]["1_30"] == 1
    assert result["counts"]["90_plus"] == 1
    assert result["totals"]["current"] == 50.0
    assert result["totals"]["1_30"] == 30.0
    assert result["totals"]["90_plus"] == 20.0
    assert result["selected_bucket"] == "1_30"
    assert result["visible_bucket_keys"] == ["1_30"]
    assert result["bucket_invoice_ids"]["1_30"]
    assert len(result["aging_trend"]["labels"]) == 6
    assert len(result["aging_trend"]["series"]["90_plus"]) == 6


def test_ar_aging_this_year_period_excludes_previous_year_invoices(db_session, subscriber):
    now = datetime.now(UTC)
    this_year_due = datetime(now.year, 1, 15, tzinfo=UTC)
    previous_year_due = datetime(now.year - 1, 12, 15, tzinfo=UTC)

    _create_invoice(db_session, account_id=subscriber.id, due_at=this_year_due, balance_due="80.00")
    _create_invoice(db_session, account_id=subscriber.id, due_at=previous_year_due, balance_due="120.00")

    all_period = build_ar_aging_data(db_session, period="all")
    this_year = build_ar_aging_data(db_session, period="this_year")

    all_total = sum(all_period["totals"].values())
    this_year_total = sum(this_year["totals"].values())

    assert all_total == 200.0
    assert this_year_total == 80.0
    assert this_year["selected_period"] == "this_year"


def test_ar_aging_top_debtors_sorted_by_total_overdue_balance(db_session):
    account_a = Subscriber(first_name="Ada", last_name="A", email="ada@example.com")
    account_b = Subscriber(first_name="Ben", last_name="B", email="ben@example.com")
    db_session.add_all([account_a, account_b])
    db_session.commit()

    now = datetime.now(UTC)
    _create_invoice(db_session, account_id=account_a.id, due_at=now - timedelta(days=31), balance_due="40.00")
    _create_invoice(db_session, account_id=account_a.id, due_at=now - timedelta(days=10), balance_due="20.00")
    _create_invoice(db_session, account_id=account_b.id, due_at=now - timedelta(days=20), balance_due="30.00")
    _create_invoice(db_session, account_id=account_b.id, due_at=now + timedelta(days=5), balance_due="999.00")

    result = build_ar_aging_data(db_session, period="all")
    top_debtors = result["top_debtors"]

    assert len(top_debtors) == 2
    assert top_debtors[0]["account_label"] == "Ada A"
    assert top_debtors[0]["amount"] == 60.0
    assert top_debtors[1]["account_label"] == "Ben B"
    assert top_debtors[1]["amount"] == 30.0


def test_ar_aging_filters_by_partner_and_location(db_session):
    reseller_a = Reseller(name="Partner A")
    reseller_b = Reseller(name="Partner B")
    db_session.add_all([reseller_a, reseller_b])
    db_session.commit()

    account_a = Subscriber(
        first_name="Lagos",
        last_name="User",
        email="lagos@example.com",
        reseller_id=reseller_a.id,
        region="Lagos",
    )
    account_b = Subscriber(
        first_name="Abuja",
        last_name="User",
        email="abuja@example.com",
        reseller_id=reseller_b.id,
        region="Abuja",
    )
    db_session.add_all([account_a, account_b])
    db_session.commit()

    now = datetime.now(UTC)
    _create_invoice(db_session, account_id=account_a.id, due_at=now - timedelta(days=12), balance_due="55.00")
    _create_invoice(db_session, account_id=account_b.id, due_at=now - timedelta(days=12), balance_due="95.00")

    filtered = build_ar_aging_data(
        db_session,
        period="all",
        partner_id=str(reseller_a.id),
        location="Lagos",
    )

    assert filtered["totals"]["1_30"] == 55.0
    assert filtered["counts"]["1_30"] == 1
    assert filtered["selected_partner_id"] == str(reseller_a.id)
    assert filtered["selected_location"] == "Lagos"


def test_ar_aging_trend_respects_partner_filter(db_session):
    reseller_a = Reseller(name="Trend Partner A")
    reseller_b = Reseller(name="Trend Partner B")
    db_session.add_all([reseller_a, reseller_b])
    db_session.commit()

    account_a = Subscriber(
        first_name="Trend",
        last_name="A",
        email="trend-a@example.com",
        reseller_id=reseller_a.id,
    )
    account_b = Subscriber(
        first_name="Trend",
        last_name="B",
        email="trend-b@example.com",
        reseller_id=reseller_b.id,
    )
    db_session.add_all([account_a, account_b])
    db_session.commit()

    now = datetime.now(UTC)
    _create_invoice(db_session, account_id=account_a.id, due_at=now - timedelta(days=10), balance_due="40.00")
    _create_invoice(db_session, account_id=account_b.id, due_at=now - timedelta(days=10), balance_due="90.00")

    filtered = build_ar_aging_data(
        db_session,
        period="all",
        partner_id=str(reseller_a.id),
    )

    latest_1_30_value = filtered["aging_trend"]["series"]["1_30"][-1]
    assert latest_1_30_value == 40.0


def test_ar_aging_top_debtors_respects_debtor_period(db_session, subscriber):
    now = datetime.now(UTC)
    this_month_due = datetime(now.year, now.month, 5, tzinfo=UTC)
    last_month_anchor = datetime(now.year, now.month, 1, tzinfo=UTC) - timedelta(days=1)
    last_month_due = datetime(last_month_anchor.year, last_month_anchor.month, 10, tzinfo=UTC)

    _create_invoice(db_session, account_id=subscriber.id, due_at=this_month_due, balance_due="33.00")
    _create_invoice(db_session, account_id=subscriber.id, due_at=last_month_due, balance_due="77.00")

    this_month = build_ar_aging_data(db_session, period="all", debtor_period="this_month")
    last_month = build_ar_aging_data(db_session, period="all", debtor_period="last_month")

    assert this_month["selected_debtor_period"] == "this_month"
    assert last_month["selected_debtor_period"] == "last_month"
    assert this_month["top_debtors"][0]["amount"] == 33.0
    assert last_month["top_debtors"][0]["amount"] == 77.0
