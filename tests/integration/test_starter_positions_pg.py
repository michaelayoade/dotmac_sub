"""Starter readers read frozen document facts and positions (PG).

A Starter-issued invoice renders its document from the exact frozen
Starter fact (number, totals, lines) through a transient overlay, its
account collectible reads from the Starter position, and a legacy
invoice has no Starter fact. The persistent mirror row is untouched
by rendering.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    StarterInvoiceLink,
    TaxRate,
)
from app.models.catalog import BillingMode
from app.services import billing_invoice_pdf
from app.services import web_billing_invoices as web_invoices
from app.services.billing import starter_policies, starter_positions
from app.services.brand_profiles import upsert_brand_profile

pytestmark = pytest.mark.integration

_SERIES = f"invread-{uuid4().hex[:8]}"


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "READ"
        if key == "starter_ledger_live_enabled":
            return True
        return real_resolve(db, domain, key)

    monkeypatch.setattr(starter_policies, "resolve_value", _resolve)


@pytest.fixture
def _seller_and_bank(db_session):
    from app.models.billing import CollectionAccount, CollectionAccountType

    upsert_brand_profile(
        db_session,
        scope_type="platform",
        scope_id=None,
        values={
            "legal_name": "Dotmac Technologies Ltd",
            "legal_address": {
                "street1": "Plot 1 Proof Way",
                "city": "Abuja",
                "country": "NGA",
            },
        },
    )
    db_session.add(
        CollectionAccount(
            name=f"Readers Collection {uuid4().hex[:8]}",
            account_type=CollectionAccountType.bank,
            bank_name="Dotmac Bank",
            account_name="Dotmac Technologies Ltd",
            account_number="0123456789",
            account_last4="6789",
            sort_code="12-34-56",
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()


def _draft_invoice(db_session, subscriber, *, total: str = "107.50") -> Invoice:
    tax_rate = TaxRate(
        name=f"Readers VAT {uuid4().hex[:8]}",
        code="vat_standard",
        rate=Decimal("0.0750"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    net = Decimal("100.00")
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=net,
        tax_total=Decimal("7.50"),
        total=Decimal(total),
        balance_due=Decimal(total),
        discount_amount=Decimal("0.00"),
        due_at=datetime.now(UTC) + timedelta(days=30),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref="readers-proof-terms",
        due_date_policy_version="1",
        is_proforma=False,
    )
    invoice.lines = [
        InvoiceLine(
            description="Fiber access (monthly)",
            quantity=Decimal("1"),
            unit_price=net,
            amount=net,
            tax_rate_id=tax_rate.id,
            tax_rate_code_snapshot="vat_standard",
            tax_rate_percent_snapshot=Decimal("7.5000"),
            tax_rate_snapshot_version=1,
            tax_rate_is_active_snapshot=True,
            is_active=True,
        )
    ]
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_starter_invoice_renders_from_frozen_fact(
    db_session, subscriber, _starter_series_isolation, _seller_and_bank
):
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    invoice = _draft_invoice(db_session, subscriber)
    issued = web_invoices.issue_invoice_from_detail(
        db_session, invoice_id=invoice.id, actor_ref="user:readers-proof"
    )
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one()
    assert link.issued_fact_version is not None

    fact = starter_positions.starter_invoice_fact(db_session, issued)
    assert fact is not None
    assert fact.document_number == issued.invoice_number
    assert fact.grand_total.amount == Decimal("107.50")

    collectible = starter_positions.starter_account_collectible(
        db_session,
        billing_account_id=link.billing_account_id,
        currency_code="NGN",
    )
    assert collectible == Decimal("107.50")

    # A stale in-memory mirror must not leak into the rendered document.
    issued.total = Decimal("999.99")
    issued.lines[0].tax_rate_percent_snapshot = Decimal("99.0000")
    overlaid = starter_positions.apply_fact_overlay(issued, fact)
    html = billing_invoice_pdf._render_invoice_html(overlaid, db_session)
    assert fact.document_number in html
    assert "107.50" in html
    assert "999.99" not in html
    assert "VAT (7.5%)" in html
    assert "VAT (99%)" not in html
    db_session.rollback()


def test_legacy_invoice_has_no_starter_fact(db_session, subscriber):
    invoice = _draft_invoice(db_session, subscriber)
    assert starter_positions.starter_invoice_fact(db_session, invoice) is None
