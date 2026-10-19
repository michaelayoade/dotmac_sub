"""Starter settlement back-half discharges Starter-issued invoices (PG).

Pay-then-record end to end on the migrated database: a Slice 6a
Starter-issued invoice is paid through the settlement lane — payments
confirmation, billing acceptance and allocation — reaching zero
collectible with a paid Sub mirror and no legacy Payment row. A second
claim against the discharged document is refused by Starter billing.
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
from dotmac_kernel.money import Currency, Money
from dotmac_payments import OpenPaymentIntent, PaymentPurpose, open_payment_intent
from sqlalchemy import select

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    StarterInvoiceLink,
    TaxRate,
)
from app.models.catalog import BillingMode
from app.services import web_billing_invoices as web_invoices
from app.services.billing import starter_issuance as issuance
from app.services.billing import starter_policies
from app.services.billing.starter_settlement import (
    StarterSettlementError,
    settle_starter_invoice_payment,
)
from app.services.brand_profiles import upsert_brand_profile
from app.services.operator_tenant import OPERATOR_TENANT_ID

pytestmark = pytest.mark.integration

_SERIES = f"invset-{uuid4().hex[:8]}"
_SCOPE = TenantScope(OPERATOR_TENANT_ID)


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "SET"
        return real_resolve(db, domain, key)

    monkeypatch.setattr(starter_policies, "resolve_value", _resolve)
    monkeypatch.setattr(issuance, "starter_ledger_live_enabled", lambda _db: True)


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
            name=f"Settlement Collection {uuid4().hex[:8]}",
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
        name=f"Settlement VAT {uuid4().hex[:8]}",
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
        due_date_basis_ref="settlement-proof-terms",
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
        db_session, invoice_id=invoice.id, actor_ref="user:settlement-proof"
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


def test_starter_settlement_discharges_invoice_without_legacy_payment(
    db_session, subscriber, _seller_and_bank, _starter_series_isolation
) -> None:
    invoice, link = _issued_invoice(db_session, subscriber)
    assert _collectible(db_session, link) == Decimal("107.50")

    reference = f"settle-proof:{uuid4().hex[:12]}"
    external_reference = f"paystack-txn-{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    open_payment_intent(
        db_session,
        scope=_SCOPE,
        command=OpenPaymentIntent(
            reference=reference,
            payer_reference=str(subscriber.id),
            purpose=PaymentPurpose.INVOICE_SETTLEMENT,
            target_reference=str(invoice.id),
            requested=Money(Decimal("107.50"), Currency("NGN", 2)),
            provider_type="paystack",
            channel="customer_selfcare",
            opened_at=now,
            expires_at=now + timedelta(hours=24),
        ),
    )
    db_session.flush()

    result = settle_starter_invoice_payment(
        db_session,
        invoice=invoice,
        link=link,
        intent_reference=reference,
        external_reference=external_reference,
        confirmed_amount=Decimal("107.50"),
        occurred_at=now,
        observed_at=now,
    )
    db_session.commit()
    db_session.refresh(invoice)

    assert result.collectible_after == Decimal("0.00")
    assert _collectible(db_session, link) == Decimal("0.00")
    assert invoice.status is InvoiceStatus.paid
    assert invoice.balance_due == Decimal("0.00")
    assert invoice.paid_at is not None
    legacy_payment = db_session.execute(
        select(Payment).where(Payment.external_id == external_reference)
    ).scalar_one_or_none()
    assert legacy_payment is None

    # An exact replay is idempotent in Starter: same confirmation and
    # settlement identities, no second allocation.
    replay = settle_starter_invoice_payment(
        db_session,
        invoice=invoice,
        link=link,
        intent_reference=reference,
        external_reference=external_reference,
        confirmed_amount=Decimal("107.50"),
        occurred_at=now,
        observed_at=now,
    )
    db_session.commit()
    assert replay.confirmation_id == result.confirmation_id
    assert replay.settlement_id == result.settlement_id
    assert _collectible(db_session, link) == Decimal("0.00")

    # Genuinely new cash against the discharged document is refused:
    # a fresh intent and confirmation cannot allocate twice.
    reference2 = f"settle-proof:{uuid4().hex[:12]}"
    open_payment_intent(
        db_session,
        scope=_SCOPE,
        command=OpenPaymentIntent(
            reference=reference2,
            payer_reference=str(subscriber.id),
            purpose=PaymentPurpose.INVOICE_SETTLEMENT,
            target_reference=str(invoice.id),
            requested=Money(Decimal("107.50"), Currency("NGN", 2)),
            provider_type="paystack",
            channel="customer_selfcare",
            opened_at=now,
            expires_at=now + timedelta(hours=24),
        ),
    )
    db_session.flush()
    with pytest.raises(StarterSettlementError):
        settle_starter_invoice_payment(
            db_session,
            invoice=invoice,
            link=link,
            intent_reference=reference2,
            external_reference=f"paystack-txn-{uuid4().hex[:12]}",
            confirmed_amount=Decimal("107.50"),
            occurred_at=now,
            observed_at=now,
        )
    db_session.rollback()
