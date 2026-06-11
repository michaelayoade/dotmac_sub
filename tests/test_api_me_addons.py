"""Customer self-service add-on purchase (app/services/customer_portal_flow_addons).

There is no add-on data in any live environment, so the wallet-charge logic is
verified here against SQLite: an add-on offered for the subscription's offer is
quoted and bought from the wallet credit balance, and an over-budget purchase is
rejected without any write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.catalog import (
    AccessType,
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferAddOn,
    OfferPrice,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.services import customer_portal_flow_addons as addons
from app.services.billing._common import get_account_credit_balance


def _make_offer(db_session, *, amount: Decimal) -> CatalogOffer:
    offer = CatalogOffer(
        name="Unlimited Lite",
        code="unlimited-lite",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        OfferPrice(
            offer_id=offer.id,
            price_type=PriceType.recurring,
            amount=amount,
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db_session.commit()
    db_session.refresh(offer)
    return offer


def _make_subscription(db_session, subscriber, offer) -> Subscription:
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        start_at=datetime.now(UTC),
        next_billing_at=datetime.now(UTC),
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


def _make_addon(
    db_session, offer, *, amount: Decimal, min_q: int = 1, max_q: int | None = None
) -> AddOn:
    add_on = AddOn(
        name="Static IP",
        addon_type=AddOnType.static_ip,
        description="A dedicated static IPv4 address.",
        is_active=True,
    )
    db_session.add(add_on)
    db_session.flush()
    db_session.add(
        AddOnPrice(
            add_on_id=add_on.id,
            price_type=PriceType.recurring,
            amount=amount,
            currency="NGN",
            billing_cycle=BillingCycle.monthly,
            is_active=True,
        )
    )
    db_session.add(
        OfferAddOn(
            offer_id=offer.id,
            add_on_id=add_on.id,
            is_required=False,
            min_quantity=min_q,
            max_quantity=max_q,
        )
    )
    db_session.commit()
    db_session.refresh(add_on)
    return add_on


def _seed_wallet_credit(db_session, subscriber, amount: Decimal) -> None:
    db_session.add(
        LedgerEntry(
            account_id=subscriber.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=amount,
            currency="NGN",
            memo="Top-up",
        )
    )
    db_session.commit()


@pytest.fixture()
def _setup(db_session, subscriber):
    offer = _make_offer(db_session, amount=Decimal("5000.00"))
    sub = _make_subscription(db_session, subscriber, offer)
    add_on = _make_addon(db_session, offer, amount=Decimal("2000.00"), max_q=3)
    customer = {"account_id": str(subscriber.id), "subscriber_id": str(subscriber.id)}
    return subscriber, sub, add_on, customer


def test_list_available_shows_offer_addon(_setup, db_session):
    _subscriber, sub, add_on, customer = _setup
    result = addons.list_available_addons(db_session, customer, str(sub.id))
    assert result is not None
    ids = [o["add_on_id"] for o in result["available"]]
    assert str(add_on.id) in ids
    option = result["available"][0]
    assert option["amount"] == 2000.0
    assert option["max_quantity"] == 3


def test_quote_computes_charge_and_affordability(_setup, db_session, subscriber):
    _subscriber, sub, add_on, customer = _setup
    _seed_wallet_credit(db_session, subscriber, Decimal("5000.00"))
    quote = addons.get_addon_quote(db_session, customer, str(sub.id), str(add_on.id), 2)
    assert quote["charge"] == Decimal("4000.00")
    assert quote["current_balance"] == Decimal("5000.00")
    assert quote["shortfall"] == Decimal("0.00")
    assert quote["can_afford"] is True


def test_purchase_charges_wallet_and_links_addon(_setup, db_session, subscriber):
    _subscriber, sub, add_on, customer = _setup
    _seed_wallet_credit(db_session, subscriber, Decimal("5000.00"))

    result = addons.purchase_addon(db_session, customer, str(sub.id), str(add_on.id), 2)
    assert result["success"] is True
    assert result["charge"] == Decimal("4000.00")
    # 5000 credit - 4000 debit = 1000 left
    assert result["new_balance"] == Decimal("1000.00")
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "1000.00"
    )
    links = (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == sub.id)
        .all()
    )
    assert len(links) == 1
    assert links[0].quantity == 2


def test_cancel_addon_ends_it(_setup, db_session, subscriber):
    _subscriber, sub, add_on, customer = _setup
    _seed_wallet_credit(db_session, subscriber, Decimal("5000.00"))
    bought = addons.purchase_addon(db_session, customer, str(sub.id), str(add_on.id), 1)
    said = bought["subscription_add_on_id"]

    assert addons.cancel_addon(db_session, customer, str(sub.id), said) is True
    sa = db_session.query(SubscriptionAddOn).filter_by(id=said).one()
    assert sa.end_at is not None
    # cancelling again is a no-op (already ended)
    assert addons.cancel_addon(db_session, customer, str(sub.id), said) is False


def test_cancel_addon_rejects_foreign(_setup, db_session, subscriber):
    import uuid

    _subscriber, sub, _add_on, customer = _setup
    assert (
        addons.cancel_addon(db_session, customer, str(sub.id), str(uuid.uuid4()))
        is False
    )


def test_purchase_is_idempotent_on_key(_setup, db_session, subscriber):
    _subscriber, sub, add_on, customer = _setup
    _seed_wallet_credit(db_session, subscriber, Decimal("5000.00"))

    first = addons.purchase_addon(
        db_session, customer, str(sub.id), str(add_on.id), 1, idempotency_key="k1"
    )
    assert first["success"] is True
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "3000.00"
    )

    # Replay with the same key — no second charge, same add-on returned.
    again = addons.purchase_addon(
        db_session, customer, str(sub.id), str(add_on.id), 1, idempotency_key="k1"
    )
    assert again["success"] is True
    assert again["replayed"] is True
    assert again["subscription_add_on_id"] == first["subscription_add_on_id"]
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "3000.00"
    )
    assert (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == sub.id)
        .count()
        == 1
    )


def test_different_keys_purchase_independently(_setup, db_session, subscriber):
    _subscriber, sub, add_on, customer = _setup
    _seed_wallet_credit(db_session, subscriber, Decimal("5000.00"))
    addons.purchase_addon(
        db_session, customer, str(sub.id), str(add_on.id), 1, idempotency_key="a"
    )
    addons.purchase_addon(
        db_session, customer, str(sub.id), str(add_on.id), 1, idempotency_key="b"
    )
    assert (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == sub.id)
        .count()
        == 2
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "1000.00"
    )


def test_idempotency_key_is_scoped_to_the_account(_setup, db_session, subscriber):
    _subscriber, sub, add_on, customer = _setup
    _seed_wallet_credit(db_session, subscriber, Decimal("5000.00"))
    addons.purchase_addon(
        db_session, customer, str(sub.id), str(add_on.id), 1, idempotency_key="shared"
    )

    from app.models.catalog import CatalogOffer
    from app.models.subscriber import Subscriber

    other = Subscriber(first_name="Other", last_name="User", email="o2@x.io")
    db_session.add(other)
    db_session.commit()
    offer = db_session.get(CatalogOffer, sub.offer_id)
    other_sub = _make_subscription(db_session, other, offer)
    _seed_wallet_credit(db_session, other, Decimal("5000.00"))
    other_customer = {"account_id": str(other.id), "subscriber_id": str(other.id)}

    # Another account reusing the same key must NOT replay the first's purchase.
    with pytest.raises(ValueError, match="already used"):
        addons.purchase_addon(
            db_session,
            other_customer,
            str(other_sub.id),
            str(add_on.id),
            1,
            idempotency_key="shared",
        )


def test_purchase_rejected_when_balance_insufficient(_setup, db_session, subscriber):
    _subscriber, sub, add_on, customer = _setup
    _seed_wallet_credit(db_session, subscriber, Decimal("1000.00"))  # < 2000

    result = addons.purchase_addon(db_session, customer, str(sub.id), str(add_on.id), 1)
    assert result["success"] is False
    assert result["reason"] == "insufficient_balance"
    assert result["shortfall"] == Decimal("1000.00")
    # nothing written
    assert (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == sub.id)
        .count()
        == 0
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "1000.00"
    )


def test_purchase_foreign_addon_rejected(_setup, db_session):
    _subscriber, sub, _add_on, customer = _setup
    import uuid

    with pytest.raises(ValueError, match="not available"):
        addons.purchase_addon(db_session, customer, str(sub.id), str(uuid.uuid4()), 1)


def test_not_owner_returns_none(_setup, db_session):
    _subscriber, sub, add_on, _customer = _setup
    stranger = {"account_id": "00000000-0000-0000-0000-000000000000"}
    assert addons.list_available_addons(db_session, stranger, str(sub.id)) is None


def test_purchase_rejected_when_subscription_not_active(_setup, db_session, subscriber):
    _subscriber, sub, add_on, customer = _setup
    _seed_wallet_credit(db_session, subscriber, Decimal("5000.00"))
    sub.status = SubscriptionStatus.suspended
    db_session.commit()

    result = addons.purchase_addon(db_session, customer, str(sub.id), str(add_on.id), 1)

    assert result["success"] is False
    assert result["reason"] == "subscription_not_active"
    assert result["subscription_status"] == "suspended"
    # nothing written: no add-on link, no wallet debit
    assert (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == sub.id)
        .count()
        == 0
    )
    assert get_account_credit_balance(db_session, str(subscriber.id)) == Decimal(
        "5000.00"
    )
