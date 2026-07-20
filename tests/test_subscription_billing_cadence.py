"""SOT: the subscription owns its billing cadence (bill per sales-order
contract, not a default monthly). Capture (sales) -> own (subscription) ->
read (biller), with the offer price as fallback-only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.models.catalog import (
    AccessType,
    BillingCycle,
    CatalogOffer,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    billing_cycle_noun,
    billing_cycle_suffix,
)
from app.models.subscriber import Subscriber
from app.services import crm_api
from app.services.billing_automation import _resolve_price
from app.services.catalog.subscriptions import _compute_next_billing_at
from app.services.sales_orders import _line_billing_cycle
from app.services.web_catalog_subscriptions import _format_offer_price_summary

_MONDAY = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def _subscriber(db) -> Subscriber:
    sub = Subscriber(
        first_name="Ada",
        last_name="L",
        email=f"a-{uuid.uuid4().hex[:8]}@x.io",
        subscriber_number=f"S-{uuid.uuid4().hex[:6]}",
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _offer(db, *, code="HOME100", price="15000", cycle=BillingCycle.monthly):
    offer = CatalogOffer(
        name="Home 100M",
        code=code,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=cycle,
        is_active=True,
    )
    db.add(offer)
    db.commit()
    db.add(
        OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            amount=Decimal(price),
            currency="NGN",
            billing_cycle=cycle,
        )
    )
    db.commit()
    return offer


# ---- pure unit ----------------------------------------------------------


def test_compute_next_billing_handles_quarterly():
    # Regression: quarterly previously fell through to monthly (+1mo).
    assert _compute_next_billing_at(_MONDAY, BillingCycle.quarterly) == datetime(
        2026, 4, 5, 12, 0, tzinfo=UTC
    )
    assert _compute_next_billing_at(_MONDAY, BillingCycle.annual) == datetime(
        2027, 1, 5, 12, 0, tzinfo=UTC
    )


def test_price_suffix_and_noun_are_cadence_aware():
    assert billing_cycle_suffix(BillingCycle.annual) == "/yr"
    assert billing_cycle_suffix(BillingCycle.quarterly) == "/qtr"
    assert billing_cycle_suffix(None) == "/mo"
    assert billing_cycle_noun(BillingCycle.quarterly) == "quarterly"
    assert billing_cycle_noun(None) == "monthly"
    assert _format_offer_price_summary(Decimal("15000"), BillingCycle.annual) == (
        "₦15,000/yr"
    )


def test_line_billing_cycle_reads_contract_metadata():
    line = SimpleNamespace(metadata_={"sub_offer_id": "x", "billing_cycle": "annual"})
    assert _line_billing_cycle(line) == "annual"
    assert _line_billing_cycle(SimpleNamespace(metadata_={})) is None
    assert _line_billing_cycle(SimpleNamespace(metadata_=None)) is None


# ---- capture -> own (DB) ------------------------------------------------


def test_create_subscription_carries_contracted_cadence(db_session):
    sub = _subscriber(db_session)
    offer = _offer(db_session)  # offer is monthly
    result = crm_api.create_subscription(
        db_session,
        subscriber_id=str(sub.id),
        offer_ref=str(offer.id),
        external_ref="so-cadence-annual",
        billing_cycle="annual",  # the sales-order line contracted annual
    )
    assert result["subscription"].billing_cycle == BillingCycle.annual


def test_create_subscription_snapshots_offer_cadence_when_uncontracted(db_session):
    sub = _subscriber(db_session)
    offer = _offer(db_session, code="HOME-Q", cycle=BillingCycle.quarterly)
    result = crm_api.create_subscription(
        db_session,
        subscriber_id=str(sub.id),
        offer_ref=str(offer.id),
        external_ref="so-cadence-snap",
        # no billing_cycle -> inherit + snapshot the offer's quarterly cadence
    )
    assert result["subscription"].billing_cycle == BillingCycle.quarterly


# ---- own -> read by biller (DB) -----------------------------------------


def test_biller_prefers_subscription_cadence_over_offer(db_session):
    sub = _subscriber(db_session)
    offer = _offer(db_session)  # offer priced monthly
    result = crm_api.create_subscription(
        db_session,
        subscriber_id=str(sub.id),
        offer_ref=str(offer.id),
        external_ref="so-cadence-biller",
        billing_cycle="annual",
    )
    subscription = result["subscription"]
    _amount, _currency, cycle = _resolve_price(db_session, subscription)
    assert cycle == BillingCycle.annual  # subscription cadence wins over monthly price


def test_biller_falls_back_to_offer_cadence_when_subscription_unset(db_session):
    # Cutover safety: a subscription with NULL billing_cycle bills on the offer
    # price cadence, exactly as before this change.
    sub = _subscriber(db_session)
    offer = _offer(db_session, code="HOME-A", cycle=BillingCycle.annual)
    result = crm_api.create_subscription(
        db_session,
        subscriber_id=str(sub.id),
        offer_ref=str(offer.id),
        external_ref="so-cadence-fallback",
    )
    subscription = result["subscription"]
    subscription.billing_cycle = None  # simulate a not-yet-backfilled legacy row
    db_session.flush()
    _amount, _currency, cycle = _resolve_price(db_session, subscription)
    assert cycle == BillingCycle.annual  # falls back to the offer price cadence
