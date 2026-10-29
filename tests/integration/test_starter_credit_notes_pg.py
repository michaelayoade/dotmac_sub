"""Fully-applied credits against Starter invoices issue in Starter (PG).

The credits lane end-to-end: a Starter-issued invoice credited in full
through the legacy preview/issue API is issued by dotmac-billing as sole
ledger writer — the Sub credit note mirrors the Starter document
number, a structural StarterCreditNoteLink records the Starter
identity, no legacy funding ledger entry exists, the Starter
collectible position falls to zero, and the invoice mirrors as paid.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_billing import (
    GetCurrentReceivablePositionV1,
    get_current_receivable_position,
)
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Currency
from sqlalchemy import func, select

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    LedgerEntry,
    StarterCreditNoteLink,
    StarterInvoiceLink,
    TaxRate,
)
from app.models.catalog import BillingMode
from app.schemas.billing import CreditNoteIssuePreviewRequest, CreditNoteIssueRequest
from app.services import web_billing_invoices as web_invoices
from app.services.billing import starter_policies
from app.services.billing.credit_notes import CreditNotes
from app.services.brand_profiles import upsert_brand_profile
from app.services.operator_tenant import OPERATOR_TENANT_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("starter_native_vat_tax_policy"),
]

_SERIES = f"invcr-{uuid4().hex[:8]}"
_SCOPE = TenantScope(OPERATOR_TENANT_ID)


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "SC"
        if key == "starter_billing_credit_note_series":
            return f"cn-{_SERIES}"
        if key == "starter_billing_credit_note_series_prefix":
            return "SCN"
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
            name=f"Credit Collection {uuid4().hex[:8]}",
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


def _issued_invoice(db_session, subscriber) -> tuple[Invoice, StarterInvoiceLink]:
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    tax_rate = TaxRate(
        name=f"Credit VAT {uuid4().hex[:8]}",
        code="vat_standard",
        rate=Decimal("0.0750"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("7.50"),
        total=Decimal("107.50"),
        balance_due=Decimal("107.50"),
        discount_amount=Decimal("0.00"),
        due_at=datetime.now(UTC) + timedelta(days=30),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref="credit-proof-terms",
        due_date_policy_version="1",
        is_proforma=False,
    )
    invoice.lines = [
        InvoiceLine(
            description="Fiber access (monthly)",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            amount=Decimal("100.00"),
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
    issued = web_invoices.issue_invoice_from_detail(
        db_session, invoice_id=invoice.id, actor_ref="user:credit-proof"
    )
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one()
    return issued, link


def _collectible(db_session, link: StarterInvoiceLink) -> Decimal:
    current = get_current_receivable_position(
        db_session,
        scope=_SCOPE,
        request=GetCurrentReceivablePositionV1(
            scope=_SCOPE,
            billing_account_id=link.billing_account_id,
            currency=Currency("NGN", 2),
        ),
    )
    assert current.position is not None
    return current.position.collectible_receivable.amount


def test_full_credit_against_starter_invoice_issues_in_starter(
    db_session, subscriber, _seller_and_bank, _starter_series_isolation
) -> None:
    invoice, link = _issued_invoice(db_session, subscriber)
    assert _collectible(db_session, link) == Decimal("107.50")
    tax_rate_id = invoice.lines[0].tax_rate_id

    preview_payload = CreditNoteIssuePreviewRequest(
        account_id=subscriber.id,
        invoice_id=invoice.id,
        credit_number=None,
        currency="NGN",
        subtotal=Decimal("100.00"),
        tax_total=Decimal("7.50"),
        total=Decimal("107.50"),
        memo="service outage goodwill",
        line_description="Fiber access credit",
        line_tax_rate_id=tax_rate_id,
    )
    preview = CreditNotes.preview_issue(db_session, preview_payload)
    result = CreditNotes.issue_with_evidence(
        db_session,
        CreditNoteIssueRequest(
            **preview_payload.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=f"credit-proof-{uuid4().hex[:16]}",
        ),
    )
    db_session.refresh(invoice)

    assert result.funding_ledger_entry is None
    assert result.credit_note.credit_number is not None
    assert result.credit_note.credit_number.startswith("SCN-")
    credit_link = db_session.execute(
        select(StarterCreditNoteLink).where(
            StarterCreditNoteLink.credit_note_id == result.credit_note.id
        )
    ).scalar_one()
    assert credit_link.original_document_id == link.document_id
    funding_count = db_session.execute(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.id == result.credit_note.funding_ledger_entry_id)
    ).scalar_one()
    assert result.credit_note.funding_ledger_entry_id is None
    assert funding_count == 0
    assert _collectible(db_session, link) == Decimal("0.00")
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
