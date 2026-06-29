"""Prepaid subscriptions must only enter invoice generation by explicit opt-in.

Production prepaid is monthly invoice-in-advance. Generic postpaid invoice paths
still exclude prepaid unless they pass ``allow_prepaid=True``.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import Invoice
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    SubscriptionStatus,
)
from app.models.subscriber import AccountStatus
from app.services import billing_automation
from app.services.billing.invoices import Invoices
from app.services.billing_automation import (
    generate_prorated_invoice,
    subscription_invoice_eligible,
)


def _add_recurring_price(db_session, offer_id, amount="100.00"):
    price = OfferPrice(
        offer_id=offer_id,
        price_type=PriceType.recurring,
        amount=Decimal(amount),
        currency="USD",
        billing_cycle=BillingCycle.monthly,
        is_active=True,
    )
    db_session.add(price)
    db_session.commit()
    return price


def _activate(db_session, subscription, subscriber_account, mode):
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = mode
    subscriber_account.status = AccountStatus.active
    subscription.start_at = now_naive - timedelta(days=30)
    subscription.next_billing_at = now_naive - timedelta(days=1)
    db_session.commit()
    return now_naive


def test_subscription_invoice_eligible_helper(db_session, subscription):
    subscription.billing_mode = BillingMode.prepaid
    assert subscription_invoice_eligible(subscription) is False
    assert subscription_invoice_eligible(subscription, allow_prepaid=True) is True
    subscription.billing_mode = BillingMode.postpaid
    assert subscription_invoice_eligible(subscription) is True


def test_invoice_cycle_skips_prepaid(db_session, subscription, subscriber_account):
    now_naive = _activate(
        db_session, subscription, subscriber_account, BillingMode.prepaid
    )
    _add_recurring_price(db_session, subscription.offer_id)

    summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

    invoices = (
        db_session.query(Invoice)
        .filter(Invoice.account_id == subscriber_account.id)
        .count()
    )
    assert invoices == 0
    assert summary["prepaid_skipped"] >= 1


def test_invoice_cycle_bills_postpaid(db_session, subscription, subscriber_account):
    now_naive = _activate(
        db_session, subscription, subscriber_account, BillingMode.postpaid
    )
    _add_recurring_price(db_session, subscription.offer_id)

    summary = billing_automation.run_invoice_cycle(db_session, run_at=now_naive)

    invoices = (
        db_session.query(Invoice)
        .filter(Invoice.account_id == subscriber_account.id)
        .count()
    )
    assert invoices >= 1
    assert summary["invoices_created"] >= 1


def test_proration_skips_prepaid(db_session, subscription, subscriber_account):
    _activate(db_session, subscription, subscriber_account, BillingMode.prepaid)
    _add_recurring_price(db_session, subscription.offer_id)
    assert generate_prorated_invoice(db_session, subscription) is None


def test_create_for_subscription_blocks_prepaid(
    db_session, subscription, subscriber_account
):
    _activate(db_session, subscription, subscriber_account, BillingMode.prepaid)
    _add_recurring_price(db_session, subscription.offer_id)

    with pytest.raises(HTTPException) as exc:
        Invoices.create_for_subscription(
            db_session, str(subscriber_account.id), str(subscription.id)
        )
    assert exc.value.status_code == 400

    # explicit override succeeds
    invoice = Invoices.create_for_subscription(
        db_session,
        str(subscriber_account.id),
        str(subscription.id),
        allow_prepaid=True,
    )
    assert invoice is not None
