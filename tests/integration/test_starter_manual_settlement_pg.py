"""Staff-recorded manual payments settle Starter invoices (PG).

Pay-then-record for the manual lane: a bound payments intent is
opened for the Starter-issued invoice, the staff evidence is recorded
as a confirmation, and billing acceptance/allocation discharges the
document — zero collectible, paid mirror, no legacy Payment row, one
access signal, and an exact replay returning the same identities.
Legacy account-credit application to the Starter invoice is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

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
from app.models.event_store import EventStore
from app.services import web_billing_invoices as web_invoices
from app.services.billing import starter_manual_settlement, starter_policies
from app.services.billing.account_credit import (
    AccountCreditApplicationError,
    AccountCreditApplications,
)
from app.services.brand_profiles import upsert_brand_profile
from app.services.events.types import EventType

pytestmark = pytest.mark.integration

_SERIES = f"invman-{uuid4().hex[:8]}"


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "MAN"
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
            name=f"Manual Collection {uuid4().hex[:8]}",
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
        name=f"Manual VAT {uuid4().hex[:8]}",
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
        due_date_basis_ref="manual-proof-terms",
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
        db_session, invoice_id=invoice.id, actor_ref="user:manual-proof"
    )
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one()
    return issued, link


def test_manual_payment_settles_starter_invoice_without_legacy_payment(
    db_session, subscriber, _starter_series_isolation, _seller_and_bank
):
    issued, link = _issued_invoice(db_session, subscriber)
    occurred_at = datetime.now(UTC)
    external_reference = f"bank-ref-{uuid4().hex[:8]}"
    idempotency_key = f"manual-{uuid4().hex}"

    def _record():
        return starter_manual_settlement.record_manual_starter_payment(
            db_session,
            invoice=issued,
            link=link,
            amount=Decimal("107.50"),
            external_reference=external_reference,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
        )

    first = _record()
    replay = _record()
    assert replay.intent_reference == first.intent_reference
    assert replay.settlement.confirmation_id == first.settlement.confirmation_id
    assert replay.settlement.settlement_id == first.settlement.settlement_id
    db_session.commit()
    db_session.refresh(issued)

    assert issued.status == InvoiceStatus.paid
    assert issued.balance_due == Decimal("0.00")
    assert first.settlement.collectible_after == Decimal("0.00")
    legacy_payments = db_session.execute(
        select(func.count()).select_from(Payment)
    ).scalar_one()
    assert legacy_payments == 0

    signals = db_session.execute(
        select(func.count())
        .select_from(EventStore)
        .where(
            EventStore.event_type == EventType.payment_received.value,
            EventStore.invoice_id == issued.id,
        )
    ).scalar_one()
    assert signals == 1

    # Legacy account credit must refuse this Starter receivable.
    with pytest.raises(AccountCreditApplicationError):
        AccountCreditApplications.apply_invoice_available(
            db_session, issued, preview_fingerprint="x" * 64
        )
    db_session.rollback()
