"""Reseller account-invoices list is routed through list_query."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Reseller, Subscriber
from app.services import reseller_portal, web_reseller_routes


def test_reseller_invoice_list_definition_declares_its_capabilities():
    definition = web_reseller_routes.RESELLER_INVOICE_LIST_DEFINITION
    assert set(definition.sortable_keys) == {
        "status",
        "total",
        "issued_at",
        "due_at",
        "created_at",
    }
    assert definition.default_sort == "issued_at"
    assert definition.default_per_page == 25
    assert 25 in definition.per_page_options


def test_invoice_definition_sort_keys_are_backed_by_the_service_whitelist():
    # Every column the UI offers to sort by must be one the service can order on,
    # or the sort would silently fall back to the default.
    for key in web_reseller_routes.RESELLER_INVOICE_LIST_DEFINITION.sortable_keys:
        assert key in reseller_portal.ACCOUNT_INVOICE_SORT_COLUMNS


def test_invoice_definition_round_trips_state_in_the_url():
    query = web_reseller_routes.RESELLER_INVOICE_LIST_DEFINITION.build_query(
        search=None,
        filters={},
        sort_by="total",
        sort_dir="asc",
        page=1,
        per_page=25,
    )
    url = query.url("/reseller/accounts/abc/invoices", page=3)
    assert "sort=total" in url
    assert "dir=asc" in url
    assert "page=3" in url


def test_invoice_projection_matches_the_template_contract(db_session):
    reseller = Reseller(name="List Contract Reseller", is_active=True)
    db_session.add(reseller)
    db_session.flush()
    account = Subscriber(
        first_name="Invoice",
        last_name="Owner",
        email="invoice-list-contract@example.com",
        reseller_id=reseller.id,
    )
    db_session.add(account)
    db_session.flush()
    issued = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    invoice = Invoice(
        account_id=account.id,
        invoice_number="INV-LIST-001",
        status=InvoiceStatus.issued,
        total=Decimal("12345.67"),
        balance_due=Decimal("2345.67"),
        issued_at=issued,
        due_at=issued + timedelta(days=7),
    )
    db_session.add(invoice)
    db_session.commit()

    rows = reseller_portal.list_account_invoices(
        db_session,
        str(reseller.id),
        str(account.id),
        order_by="issued_at",
    )

    assert rows is not None
    assert rows[0]["total"] == Decimal("12345.67")
    # SQLite drops timezone metadata; the date/time value remains authoritative.
    assert rows[0]["issued_at"].replace(tzinfo=UTC) == issued
    assert rows[0]["due_at"].replace(tzinfo=UTC) == issued + timedelta(days=7)
    source = (
        Path(__file__).resolve().parents[1]
        / "templates/reseller/accounts/invoices.html"
    ).read_text(encoding="utf-8")
    assert "invoice.total" in source
    assert "invoice.issued_at" in source
    assert "invoice.due_at" in source
    assert "list_pagination(list_query, page_meta" in source
