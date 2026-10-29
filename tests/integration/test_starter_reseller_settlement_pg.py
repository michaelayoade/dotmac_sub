"""One reseller confirmation settles many subscribers in Starter (PG).

Pay-then-record for the consolidated lane: a single bound payments
intent (payer = reseller party) is confirmed once, then accepted and
allocated as exact per-subscriber slices on each subscriber's own
Starter billing account — zero collectible, paid mirrors, one access
signal per discharged invoice, and an exact replay returning the
same confirmation identity without second postings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.billing import (
    BillingAccount,
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    StarterInvoiceLink,
    TaxRate,
)
from app.models.catalog import BillingMode
from app.models.event_store import EventStore
from app.models.party import Party
from app.models.subscriber import Reseller, Subscriber
from app.services import web_billing_invoices as web_invoices
from app.services.billing import starter_policies, starter_reseller_settlement
from app.services.billing.starter_reseller_settlement import (
    ResellerSettlementSlice,
    settle_reseller_consolidated,
)
from app.services.brand_profiles import upsert_brand_profile
from app.services.events.types import EventType

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("starter_native_vat_tax_policy"),
]

_SERIES = f"invres-{uuid4().hex[:8]}"


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "RES"
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
            name=f"Reseller Collection {uuid4().hex[:8]}",
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


def _reseller_account(db_session) -> BillingAccount:
    party = Party(party_type="organization", display_name="Reseller Org")
    db_session.add(party)
    db_session.flush()
    reseller = Reseller(
        name=f"R-{uuid4().hex[:6]}",
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="test",
        party_binding_reason="pg-proof",
    )
    db_session.add(reseller)
    db_session.flush()
    account = BillingAccount(
        reseller_id=reseller.id,
        name=f"BA-{uuid4().hex[:6]}",
        currency="NGN",
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _member(db_session, reseller_id) -> Subscriber:
    sub = Subscriber(
        first_name="M",
        last_name="Ember",
        email=f"m{uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
        reseller_id=reseller_id,
        billing_mode=BillingMode.postpaid,
        billing_address_line1="2 Customer Close",
        billing_city="Abuja",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _issued_invoice(db_session, subscriber) -> Invoice:
    tax_rate = TaxRate(
        name=f"Reseller VAT {uuid4().hex[:8]}",
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
        due_date_basis_ref="reseller-proof-terms",
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
        db_session, invoice_id=invoice.id, actor_ref="user:reseller-proof"
    )
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one()
    assert link is not None
    return issued


def test_one_confirmation_settles_two_subscribers_exactly_once(
    db_session, _starter_series_isolation, _seller_and_bank
):
    account = _reseller_account(db_session)
    first = _member(db_session, account.reseller_id)
    second = _member(db_session, account.reseller_id)
    invoice_a = _issued_invoice(db_session, first)
    # A second invoice for the same subscriber: distinct per-invoice
    # settlement identity under the one confirmation.
    invoice_a2 = _issued_invoice(db_session, first)
    invoice_b = _issued_invoice(db_session, second)
    payment_id = uuid4()
    external_reference = f"reseller-bank-{uuid4().hex[:8]}"
    occurred_at = datetime.now(UTC)
    slices = (
        ResellerSettlementSlice(
            invoice_id=invoice_a.id,
            subscriber_id=first.id,
            amount=Decimal("107.50"),
        ),
        ResellerSettlementSlice(
            invoice_id=invoice_a2.id,
            subscriber_id=first.id,
            amount=Decimal("107.50"),
        ),
        ResellerSettlementSlice(
            invoice_id=invoice_b.id,
            subscriber_id=second.id,
            amount=Decimal("107.50"),
        ),
    )

    reason = starter_reseller_settlement.reseller_settlement_eligibility_reason(
        db_session,
        account=account,
        slices=slices,
        total=Decimal("322.50"),
        external_reference=external_reference,
    )
    assert reason is None

    def _settle():
        return settle_reseller_consolidated(
            db_session,
            account=account,
            payment_id=payment_id,
            slices=slices,
            external_reference=external_reference,
            confirmed_amount=Decimal("322.50"),
            occurred_at=occurred_at,
        )

    result = _settle()
    db_session.commit()
    replay = _settle()
    db_session.commit()
    assert replay.confirmation_id == result.confirmation_id
    assert set(result.settled_invoice_ids) == {
        invoice_a.id,
        invoice_a2.id,
        invoice_b.id,
    }

    for invoice in (invoice_a, invoice_a2, invoice_b):
        db_session.refresh(invoice)
        assert invoice.status is InvoiceStatus.paid
        assert invoice.balance_due == Decimal("0.00")
        signals = db_session.execute(
            select(func.count())
            .select_from(EventStore)
            .where(
                EventStore.event_type == EventType.payment_received.value,
                EventStore.invoice_id == invoice.id,
            )
        ).scalar_one()
        assert signals == 1
