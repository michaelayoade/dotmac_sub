"""Web-detail Issue switches eligible drafts to Starter (migrated PG).

Slice 6a end-to-end: an eligible postpaid single-line draft issued from
the admin detail path is written by dotmac-billing as sole ledger
writer — the Sub row mirrors the Starter document number/status and a
structural StarterInvoiceLink records the Starter identities. An
ineligible draft (prepaid subscriber) keeps the legacy writer and gets
no link row.
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
from sqlalchemy import select

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    StarterInvoiceLink,
)
from app.models.catalog import BillingMode
from app.services import web_billing_invoices as web_invoices
from app.services.billing import starter_policies
from app.services.brand_profiles import upsert_brand_profile
from app.services.operator_tenant import OPERATOR_TENANT_ID

pytestmark = pytest.mark.integration

_SERIES = f"inv6a-{uuid4().hex[:8]}"


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "S6A"
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
            name=f"Slice6a Collection {uuid4().hex[:8]}",
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


def _draft(db_session, subscriber, *, total: str = "107.50") -> Invoice:
    from app.models.billing import TaxRate

    tax_rate = TaxRate(
        name=f"Slice6a VAT {uuid4().hex[:8]}",
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
        total=Decimal(total),
        balance_due=Decimal(total),
        discount_amount=Decimal("0.00"),
        due_at=datetime.now(UTC) + timedelta(days=30),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref="slice6a-proof-terms",
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
    db_session.refresh(invoice)
    return invoice


def test_eligible_draft_issues_through_starter(
    db_session, subscriber, _seller_and_bank, _starter_series_isolation
) -> None:
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    db_session.commit()
    invoice = _draft(db_session, subscriber)

    issued = web_invoices.issue_invoice_from_detail(
        db_session, invoice_id=invoice.id, actor_ref="user:slice6a-proof"
    )

    assert issued.status is InvoiceStatus.issued
    assert issued.invoice_number is not None
    assert issued.invoice_number.startswith("S6A-")
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one()
    assert link.document_id is not None

    current = get_current_receivable_position(
        db_session,
        scope=TenantScope(OPERATOR_TENANT_ID),
        request=GetCurrentReceivablePositionV1(
            scope=TenantScope(OPERATOR_TENANT_ID),
            billing_account_id=link.billing_account_id,
            currency=Currency("NGN", 2),
        ),
    )
    assert current.position is not None
    assert current.position.collectible_receivable.amount == Decimal("107.50")


def test_homogeneous_multiline_draft_issues_through_starter(
    db_session, subscriber, _seller_and_bank, _starter_series_isolation
) -> None:
    from app.models.billing import StarterInvoiceLineLink, TaxRate

    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    tax_rate = TaxRate(
        name=f"Multiline VAT {uuid4().hex[:8]}",
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
        subtotal=Decimal("150.00"),
        tax_total=Decimal("11.25"),
        total=Decimal("161.25"),
        balance_due=Decimal("161.25"),
        discount_amount=Decimal("0.00"),
        due_at=datetime.now(UTC) + timedelta(days=30),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref="multiline-proof-terms",
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
        ),
        InvoiceLine(
            description="Router rental",
            quantity=Decimal("2"),
            unit_price=Decimal("25.00"),
            amount=Decimal("50.00"),
            tax_rate_id=tax_rate.id,
            tax_rate_code_snapshot="vat_standard",
            tax_rate_percent_snapshot=Decimal("7.5000"),
            tax_rate_snapshot_version=1,
            tax_rate_is_active_snapshot=True,
            is_active=True,
        ),
    ]
    db_session.add(invoice)
    db_session.commit()
    db_session.refresh(invoice)

    issued = web_invoices.issue_invoice_from_detail(
        db_session, invoice_id=invoice.id, actor_ref="user:multiline-proof"
    )

    assert issued.status is InvoiceStatus.issued
    assert issued.invoice_number is not None
    assert issued.invoice_number.startswith("S6A-")
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one()
    assert link.document_id is not None
    assert link.obligation_id is None
    line_links = (
        db_session.execute(
            select(StarterInvoiceLineLink).where(
                StarterInvoiceLineLink.invoice_line_id.in_(
                    [line.id for line in invoice.lines]
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(line_links) == 2
    assert len({row.obligation_id for row in line_links}) == 2

    current = get_current_receivable_position(
        db_session,
        scope=TenantScope(OPERATOR_TENANT_ID),
        request=GetCurrentReceivablePositionV1(
            scope=TenantScope(OPERATOR_TENANT_ID),
            billing_account_id=link.billing_account_id,
            currency=Currency("NGN", 2),
        ),
    )
    assert current.position is not None
    assert current.position.collectible_receivable.amount == Decimal("161.25")


def test_multiline_draft_stays_on_legacy_writer(
    db_session, subscriber, _seller_and_bank, _starter_series_isolation
) -> None:
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    db_session.commit()
    invoice = _draft(db_session, subscriber)
    invoice.lines.append(
        InvoiceLine(
            description="Router rental",
            quantity=Decimal("1"),
            unit_price=Decimal("0.00"),
            amount=Decimal("0.00"),
            is_active=True,
        )
    )
    db_session.commit()

    issued = web_invoices.issue_invoice_from_detail(
        db_session, invoice_id=invoice.id, actor_ref="user:slice6a-proof"
    )

    assert issued.status is InvoiceStatus.issued
    # The legacy writer numbers outside this transition in the migrated
    # test database; the lane discriminator is the absence of a Starter
    # number and, structurally, of a StarterInvoiceLink.
    assert not (issued.invoice_number or "").startswith("S6A-")
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one_or_none()
    assert link is None
