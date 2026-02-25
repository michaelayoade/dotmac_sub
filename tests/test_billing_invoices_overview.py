from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Reseller, Subscriber
from app.services.web_billing_overview import build_invoices_list_data, render_invoices_csv


def _create_invoice(
    db_session,
    *,
    account_id,
    invoice_number: str,
    total: str,
    balance_due: str,
    status: InvoiceStatus,
    created_at: datetime,
):
    invoice = Invoice(
        account_id=account_id,
        invoice_number=invoice_number,
        status=status,
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(balance_due),
        created_at=created_at,
    )
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)
    return invoice


def test_invoices_list_returns_status_totals_and_payment_split(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-100",
        total="100.00",
        balance_due="25.00",
        status=InvoiceStatus.partially_paid,
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-101",
        total="80.00",
        balance_due="0.00",
        status=InvoiceStatus.paid,
        created_at=now,
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=None,
        status=None,
        customer_ref=None,
        search=None,
        date_range=None,
        page=1,
        per_page=25,
    )

    assert result["status_totals"]["partially_paid"]["count"] == 1
    assert result["status_totals"]["paid"]["count"] == 1
    assert result["status_totals"]["all"]["count"] == 2
    assert result["status_totals"]["all"]["due_total"] == 25.0
    assert result["status_totals"]["all"]["received_total"] == 155.0


def test_invoices_list_search_filters_invoice_numbers(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-MATCH-1",
        total="30.00",
        balance_due="30.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-OTHER-2",
        total="50.00",
        balance_due="50.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=None,
        status=None,
        customer_ref=None,
        search="MATCH",
        date_range=None,
        page=1,
        per_page=25,
    )

    assert result["total"] == 1
    assert len(result["invoices"]) == 1
    assert result["invoices"][0].invoice_number == "INV-MATCH-1"


def test_invoices_list_date_range_filters_recent_rows(db_session, subscriber):
    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-RECENT",
        total="45.00",
        balance_due="45.00",
        status=InvoiceStatus.issued,
        created_at=now - timedelta(days=1),
    )
    _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-OLD",
        total="60.00",
        balance_due="60.00",
        status=InvoiceStatus.issued,
        created_at=now - timedelta(days=50),
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=None,
        status=None,
        customer_ref=None,
        search=None,
        date_range="month",
        page=1,
        per_page=25,
    )

    numbers = {item.invoice_number for item in result["invoices"]}
    assert "INV-RECENT" in numbers
    assert "INV-OLD" not in numbers


def test_invoices_list_filters_by_partner(db_session):
    reseller_a = Reseller(name="Partner A")
    reseller_b = Reseller(name="Partner B")
    db_session.add_all([reseller_a, reseller_b])
    db_session.commit()

    account_a = Subscriber(
        first_name="Inv",
        last_name="A",
        email="inv-a@example.com",
        reseller_id=reseller_a.id,
    )
    account_b = Subscriber(
        first_name="Inv",
        last_name="B",
        email="inv-b@example.com",
        reseller_id=reseller_b.id,
    )
    db_session.add_all([account_a, account_b])
    db_session.commit()

    now = datetime.now(UTC)
    _create_invoice(
        db_session,
        account_id=account_a.id,
        invoice_number="INV-PA-1",
        total="75.00",
        balance_due="75.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )
    _create_invoice(
        db_session,
        account_id=account_b.id,
        invoice_number="INV-PB-1",
        total="95.00",
        balance_due="95.00",
        status=InvoiceStatus.issued,
        created_at=now,
    )

    result = build_invoices_list_data(
        db_session,
        account_id=None,
        partner_id=str(reseller_a.id),
        status=None,
        customer_ref=None,
        search=None,
        date_range=None,
        page=1,
        per_page=25,
    )

    assert result["total"] == 1
    assert len(result["invoices"]) == 1
    assert result["invoices"][0].invoice_number == "INV-PA-1"
    assert result["selected_partner_id"] == str(reseller_a.id)


def test_render_invoices_csv_contains_due_and_received_columns(db_session, subscriber):
    now = datetime.now(UTC)
    invoice = _create_invoice(
        db_session,
        account_id=subscriber.id,
        invoice_number="INV-CSV-1",
        total="150.00",
        balance_due="40.00",
        status=InvoiceStatus.partially_paid,
        created_at=now,
    )

    csv_text = render_invoices_csv([invoice])

    assert "invoice_id,invoice_number,account_id,status,total,balance_due,payment_received,currency" in csv_text
    assert "INV-CSV-1" in csv_text
    assert ",150.00,40.00,110.00,NGN," in csv_text
