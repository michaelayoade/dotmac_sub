from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.catalog import (
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingCycle,
    BillingMode,
    OfferPrice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.schemas.catalog import (
    AddOnPriceUpdate,
    CatalogOfferUpdate,
    OfferPriceCreate,
    OfferPriceUpdate,
)
from app.services import catalog as catalog_service


def _live_subscription(db, subscriber, offer):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        start_at=datetime.now(UTC) - timedelta(days=5),
        next_billing_at=datetime.now(UTC) + timedelta(days=25),
    )
    db.add(subscription)
    db.commit()
    return subscription


def test_live_offer_cadence_cannot_be_mutated_in_place(
    db_session, subscriber, catalog_offer
):
    _live_subscription(db_session, subscriber, catalog_offer)

    with pytest.raises(HTTPException) as exc:
        catalog_service.offers.update(
            db_session,
            str(catalog_offer.id),
            CatalogOfferUpdate(billing_cycle=BillingCycle.annual),
        )

    db_session.refresh(catalog_offer)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "live_catalog_billing_mutation_blocked"
    assert catalog_offer.billing_cycle == BillingCycle.monthly


def test_live_offer_price_cannot_be_mutated_in_place(
    db_session, subscriber, catalog_offer
):
    price = OfferPrice(
        offer_id=catalog_offer.id,
        price_type=PriceType.recurring,
        amount=Decimal("100.00"),
        currency="NGN",
        billing_cycle=BillingCycle.monthly,
        is_active=True,
    )
    db_session.add(price)
    db_session.commit()
    _live_subscription(db_session, subscriber, catalog_offer)

    with pytest.raises(HTTPException) as exc:
        catalog_service.offer_prices.update(
            db_session,
            str(price.id),
            OfferPriceUpdate(amount=Decimal("200.00")),
        )

    db_session.refresh(price)
    assert exc.value.status_code == 409
    assert price.amount == Decimal("100.00")


def test_duplicate_active_offer_price_is_rejected(db_session, catalog_offer):
    db_session.add(
        OfferPrice(
            offer_id=catalog_offer.id,
            price_type=PriceType.recurring,
            amount=Decimal("100.00"),
            currency="NGN",
            is_active=True,
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        catalog_service.offer_prices.create(
            db_session,
            OfferPriceCreate(
                offer_id=catalog_offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("200.00"),
                currency="NGN",
            ),
        )

    assert exc.value.detail["code"] == "duplicate_active_offer_price"


def test_live_add_on_price_cannot_be_mutated_in_place(
    db_session, subscriber, catalog_offer
):
    subscription = _live_subscription(db_session, subscriber, catalog_offer)
    add_on = AddOn(name="Static IP", addon_type=AddOnType.custom, is_active=True)
    db_session.add(add_on)
    db_session.flush()
    price = AddOnPrice(
        add_on_id=add_on.id,
        price_type=PriceType.recurring,
        amount=Decimal("100.00"),
        currency="NGN",
        is_active=True,
    )
    db_session.add(price)
    db_session.add(
        SubscriptionAddOn(
            subscription_id=subscription.id,
            add_on_id=add_on.id,
            start_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        catalog_service.add_on_prices.update(
            db_session,
            str(price.id),
            AddOnPriceUpdate(amount=Decimal("150.00")),
        )

    db_session.refresh(price)
    assert exc.value.detail["code"] == "live_catalog_billing_mutation_blocked"
    assert price.amount == Decimal("100.00")


def test_nonfinancial_offer_edit_remains_available(
    db_session, subscriber, catalog_offer
):
    _live_subscription(db_session, subscriber, catalog_offer)

    updated = catalog_service.offers.update(
        db_session,
        str(catalog_offer.id),
        CatalogOfferUpdate(description="Updated service description"),
    )

    assert updated.description == "Updated service description"


def test_billing_catalog_change_records_actor_and_audit(db_session, catalog_offer):
    catalog_service.offers.update(
        db_session,
        str(catalog_offer.id),
        CatalogOfferUpdate(billing_cycle=BillingCycle.annual),
        actor_id="operator-123",
        actor_type="system_user",
    )

    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "catalog_offer")
        .filter(AuditEvent.entity_id == str(catalog_offer.id))
        .filter(AuditEvent.action == "catalog_billing_updated")
        .order_by(AuditEvent.occurred_at.desc())
        .first()
    )
    assert event is not None
    assert event.actor_id == "operator-123"
    assert event.actor_type.value == "user"
    assert event.metadata_["changes"]["billing_cycle"] == "annual"
