"""Scheduled cycle invoices issue through Starter when eligible (PG).

The recurring lane end-to-end: the postpaid invoice cycle stages a draft,
appends the subscription line, and at end-of-run issues the eligible
draft through dotmac-billing as sole ledger writer — the Sub row mirrors
the Starter document number/status, a structural StarterInvoiceLink
records the Starter identities, and the Starter collectible position
carries the cycle total. A second subscription for the same subscriber
and period must not append legacy lines to the Starter-owned invoice;
it bills on its own invoice.
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

from app.models.billing import Invoice, InvoiceStatus, StarterInvoiceLink
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionStatus,
)
from app.models.subscriber import SubscriberStatus
from app.services import billing_automation
from app.services.billing import starter_policies
from app.services.brand_profiles import upsert_brand_profile
from app.services.operator_tenant import OPERATOR_TENANT_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("starter_native_zero_tax_policy"),
]

_SERIES = f"invr-{uuid4().hex[:8]}"
# D1 switch as the isolation resolver reports it; the OFF test flips it.
_LEDGER_LIVE = {"enabled": True}


@pytest.fixture
def _starter_series_isolation(monkeypatch):
    real_resolve = starter_policies.resolve_value

    def _resolve(db, domain, key):
        if key == "starter_billing_invoice_series":
            return _SERIES
        if key == "starter_billing_invoice_series_prefix":
            return "SR"
        if key == "starter_ledger_live_enabled":
            return _LEDGER_LIVE["enabled"]
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
            name=f"Recurring Collection {uuid4().hex[:8]}",
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


def _billable(db_session, subscription, subscriber, *, amount: str = "100.00"):
    now = datetime.now(UTC)
    subscriber.billing_mode = BillingMode.postpaid
    subscriber.status = SubscriberStatus.active
    subscriber.billing_address_line1 = "2 Customer Close"
    subscriber.billing_city = "Abuja"
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.postpaid
    subscription.start_at = now - timedelta(days=30)
    subscription.next_billing_at = now - timedelta(days=1)
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal(amount),
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db_session.commit()
    return now


def test_cycle_invoice_issues_through_starter(
    db_session, subscription, subscriber, _seller_and_bank, _starter_series_isolation
) -> None:
    now = _billable(db_session, subscription, subscriber)

    summary = billing_automation.run_invoice_cycle(
        db_session, run_at=now, run_prepaid_renewals=False
    )

    assert summary["starter_issued_invoices"] >= 1
    invoice = db_session.execute(
        select(Invoice).where(Invoice.account_id == subscriber.id)
    ).scalar_one()
    assert invoice.status is InvoiceStatus.issued
    assert invoice.invoice_number is not None
    assert invoice.invoice_number.startswith("SR-")
    link = db_session.execute(
        select(StarterInvoiceLink).where(StarterInvoiceLink.invoice_id == invoice.id)
    ).scalar_one()
    assert link.document_id is not None
    assert link.source_fact_id == f"scheduled-billing-invoice:{invoice.id}"

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
    assert current.position.collectible_receivable.amount == invoice.total


def test_cycle_invoice_stays_legacy_when_switch_off(
    db_session,
    subscription,
    subscriber,
    _seller_and_bank,
    _starter_series_isolation,
    monkeypatch,
) -> None:
    monkeypatch.setitem(_LEDGER_LIVE, "enabled", False)
    now = _billable(db_session, subscription, subscriber)

    summary = billing_automation.run_invoice_cycle(
        db_session, run_at=now, run_prepaid_renewals=False
    )

    assert summary["starter_issued_invoices"] == 0
    invoice = db_session.execute(
        select(Invoice).where(Invoice.account_id == subscriber.id)
    ).scalar_one()
    assert not (invoice.invoice_number or "").startswith("SR-")
    links = (
        db_session.execute(
            select(StarterInvoiceLink).where(
                StarterInvoiceLink.invoice_id == invoice.id
            )
        )
        .scalars()
        .all()
    )
    assert links == []
