"""Prepaid purchases earmark periods and settle them in Starter (PG).

A confirmed purchase-funding intent earmarks two periods; the first
period invoice issues through the prepaid flavor and settles to zero
collectible from its own earmark (paid mirror, one access signal, no
legacy ledger entry), while an over-amount second-period allocation
is refused by Starter billing. A funding replay returns the same
identities.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from dotmac_kernel.money import Currency, Money
from dotmac_payments import OpenPaymentIntent, PaymentPurpose, open_payment_intent
from dotmac_kernel.cache import TenantScope
from sqlalchemy import func, select

from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    StarterInvoiceLink,
    StarterPrepaidFundingLink,
    TaxRate,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.event_store import EventStore
from app.services.billing import starter_policies, starter_prepaid
from app.services.brand_profiles import upsert_brand_profile
from app.services.events.types import EventType
from app.services.operator_tenant import OPERATOR_TENANT_ID

pytestmark = pytest.mark.integration

_SERIES = f"invprep-{uuid4().hex[:8]}"
_SCOPE = TenantScope(OPERATOR_TENANT_ID)
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
_KEY1 = "prepaid-renewal:period-1"
_KEY2 = "prepaid-renewal:period-2"


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "PRE"
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
            name=f"Prepaid Collection {uuid4().hex[:8]}",
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


def _period_draft(db_session, subscriber, subscription, *, key: str, total: str):
    tax_rate = TaxRate(
        name=f"Prepaid VAT {uuid4().hex[:8]}",
        code="vat_standard",
        rate=Decimal("0.0750"),
        is_active=True,
    )
    db_session.add(tax_rate)
    db_session.flush()
    net = (Decimal(total) / Decimal("1.075")).quantize(Decimal("0.01"))
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.draft,
        currency="NGN",
        subtotal=net,
        tax_total=Decimal(total) - net,
        total=Decimal(total),
        balance_due=Decimal(total),
        discount_amount=Decimal("0.00"),
        due_at=_NOW + timedelta(days=30),
        is_proforma=False,
    )
    invoice.lines = [
        InvoiceLine(
            description="Fiber access (prepaid month)",
            quantity=Decimal("1"),
            unit_price=net,
            amount=net,
            subscription_id=subscription.id,
            billing_line_key=key,
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


def test_prepaid_purchase_earmarks_and_settles_first_period(
    db_session, subscriber, subscription, _starter_series_isolation, _seller_and_bank
):
    subscriber.billing_mode = BillingMode.prepaid
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    db_session.commit()

    purchase_ref = f"purchase-{uuid4().hex[:8]}"
    intent_ref = f"topup-{uuid4().hex[:8]}"
    open_payment_intent(
        db_session,
        scope=_SCOPE,
        command=OpenPaymentIntent(
            reference=intent_ref,
            payer_reference=str(subscriber.id),
            purpose=PaymentPurpose.PURCHASE_FUNDING,
            target_reference=purchase_ref,
            requested=Money(Decimal("215.00"), Currency("NGN", 2)),
            provider_type="paystack",
            channel="checkout",
            opened_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        ),
    )
    targets = (
        starter_prepaid.PrepaidPeriodTarget(
            subscription_id=subscription.id,
            billing_line_key=_KEY1,
            amount=Decimal("107.50"),
        ),
        starter_prepaid.PrepaidPeriodTarget(
            subscription_id=subscription.id,
            billing_line_key=_KEY2,
            amount=Decimal("107.50"),
        ),
    )

    def _fund():
        return starter_prepaid.fund_prepaid_purchase(
            db_session,
            subscriber_id=subscriber.id,
            purchase_ref=purchase_ref,
            intent_reference=intent_ref,
            external_reference=f"ps-{purchase_ref}",
            confirmed_amount=Decimal("215.00"),
            currency_code="NGN",
            targets=targets,
            occurred_at=_NOW,
        )

    funding = _fund()
    db_session.commit()
    replay = _fund()
    assert replay.settlement_id == funding.settlement_id
    assert len(replay.links) == 2

    draft = _period_draft(
        db_session, subscriber, subscription, key=_KEY1, total="107.50"
    )
    link = starter_prepaid.funding_link_for_invoice(db_session, draft)
    assert link is not None
    settled = starter_prepaid.settle_prepaid_period_invoice(
        db_session, invoice=draft, link=link, issued_at=_NOW
    )
    db_session.commit()
    db_session.refresh(draft)

    assert draft.status == InvoiceStatus.paid
    assert draft.balance_due == Decimal("0.00")
    assert settled.collectible_after == Decimal("0.00")
    assert draft.invoice_number is not None and draft.invoice_number.startswith("PRE-")
    starter_link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == draft.id)
    ).scalar_one()
    assert starter_link.source_fact_id == f"prepaid-period:{_KEY1}"
    signals = db_session.execute(
        select(func.count())
        .select_from(EventStore)
        .where(
            EventStore.event_type == EventType.payment_received.value,
            EventStore.invoice_id == draft.id,
        )
    ).scalar_one()
    assert signals == 1

    # Period two's earmark (107.50) cannot fund an over-amount draft.
    over = _period_draft(
        db_session, subscriber, subscription, key=_KEY2, total="215.00"
    )
    over_link = starter_prepaid.funding_link_for_invoice(db_session, over)
    assert over_link is not None
    with pytest.raises(starter_prepaid.StarterPrepaidError):
        starter_prepaid.settle_prepaid_period_invoice(
            db_session, invoice=over, link=over_link, issued_at=_NOW
        )
    db_session.rollback()

    links = db_session.execute(
        select(func.count()).select_from(StarterPrepaidFundingLink)
    ).scalar_one()
    assert links == 2
