from __future__ import annotations

from decimal import Decimal

from app.models.catalog import OfferPrice, PriceType, SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from app.services.customer_chargeability import (
    ChargeabilityReason,
    CustomerChargeabilityStatus,
    resolve_customer_chargeability,
)


def _classify(db_session, subscriber, subscription):
    return resolve_customer_chargeability(db_session, (subscriber.id,))[subscriber.id]


def test_explicit_zero_catalog_price_is_confirmed_non_billable(
    db_session, subscriber, subscription
):
    subscriber.status = SubscriberStatus.delinquent
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = Decimal("0.00")
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("0.00"),
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()

    result = _classify(db_session, subscriber, subscription)

    assert result.status is CustomerChargeabilityStatus.confirmed_non_billable
    assert result.reasons == (ChargeabilityReason.explicit_zero_price,)
    assert result.appears_in_non_billable_section is True


def test_missing_catalog_price_is_review_required_not_assumed_free(
    db_session, subscriber, subscription
):
    subscription.status = SubscriptionStatus.disabled
    subscription.unit_price = None
    db_session.commit()

    result = _classify(db_session, subscriber, subscription)

    assert result.status is CustomerChargeabilityStatus.review_required
    assert result.reasons == (ChargeabilityReason.missing_catalog_price,)
    assert result.appears_in_non_billable_section is True


def test_any_chargeable_service_keeps_account_billable(
    db_session, subscriber, subscription
):
    subscription.status = SubscriptionStatus.blocked
    subscription.unit_price = Decimal("2500.00")
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("2500.00"),
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()

    result = _classify(db_session, subscriber, subscription)

    assert result.status is CustomerChargeabilityStatus.billable
    assert result.reasons == (ChargeabilityReason.chargeable_service,)


def test_multiple_active_catalog_prices_require_review(
    db_session, subscriber, subscription
):
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = Decimal("2500.00")
    db_session.add_all(
        [
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("2500.00"),
                currency="NGN",
                is_active=True,
            ),
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=Decimal("3000.00"),
                currency="NGN",
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    result = _classify(db_session, subscriber, subscription)

    assert result.status is CustomerChargeabilityStatus.review_required
    assert result.reasons == (ChargeabilityReason.multiple_active_catalog_prices,)


def test_zero_catalog_with_positive_subscription_price_requires_review(
    db_session, subscriber, subscription
):
    subscription.status = SubscriptionStatus.active
    subscription.unit_price = Decimal("2500.00")
    db_session.add(
        OfferPrice(
            offer_id=subscription.offer_id,
            price_type=PriceType.recurring,
            amount=Decimal("0.00"),
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()

    result = _classify(db_session, subscriber, subscription)

    assert result.status is CustomerChargeabilityStatus.review_required
    assert result.reasons == (ChargeabilityReason.catalog_subscription_price_mismatch,)
