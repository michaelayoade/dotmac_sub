"""Splynx → add-on importer (core, no live Splynx connection).

Sample rows mirror the real Splynx ``tariffs_one_time`` / ``tariffs_custom``
shapes; the import is verified against SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models.catalog import (
    AccessType,
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferAddOn,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.services.migrations.sync_addons_from_splynx import (
    import_addon_rows,
    ip_prefix_length,
    seed_ip_addon_offer_links,
)

_ONE_TIME = [
    {
        "id": 1,
        "title": "Air Fibre Installation Cost",
        "service_description": "",
        "price": Decimal("30000.00"),
        "deleted": "0",
        "enabled": "1",
    },
    {
        "id": 2,
        "title": "Call down support",
        "service_description": "For support",
        "price": Decimal("5000.00"),
        "deleted": "0",
        "enabled": "1",
    },
    {
        "id": 13,
        "title": "Device Replacement",
        "service_description": "",
        "price": Decimal("50000.00"),
        "deleted": "0",
        "enabled": "1",
    },
    {"id": 99, "title": "Old thing", "price": Decimal("1.00"), "deleted": "1"},
]

_CUSTOM = [
    {"id": 8, "title": "/32 IP", "price": Decimal("2687.50"), "deleted": "0"},
    {"id": 9, "title": "/30 IP", "price": Decimal("10750.00"), "deleted": "0"},
    {"id": 3, "title": "Unlimited 10", "price": Decimal("75250.00"), "deleted": "0"},
]


def test_ip_prefix_length():
    assert ip_prefix_length("/29 IP") == 29
    assert ip_prefix_length("/28 IP ") == 28
    assert ip_prefix_length("/32 IP") == 32
    assert ip_prefix_length("Unlimited 10") is None
    assert ip_prefix_length("/40 IP") is None  # out of range


def test_imports_one_time_and_ip_blocks(db_session):
    summary = import_addon_rows(db_session, _ONE_TIME, _CUSTOM)
    assert summary == {"one_time": 3, "ip_blocks": 2, "skipped": 2}

    by_source = {a.splynx_source: a for a in db_session.query(AddOn).all()}
    # one-time classification
    assert by_source["one_time:1"].addon_type == AddOnType.install_fee
    assert by_source["one_time:2"].addon_type == AddOnType.premium_support
    assert by_source["one_time:13"].addon_type == AddOnType.custom
    assert by_source["one_time:1"].ip_is_public is False

    # IP blocks
    ip32 = by_source["custom:8"]
    assert ip32.addon_type == AddOnType.static_ip
    assert ip32.ip_is_public is True
    assert ip32.ip_prefix_length == 32
    assert by_source["custom:9"].addon_type == AddOnType.extra_ip
    assert by_source["custom:9"].ip_prefix_length == 30

    # plan-shaped custom + deleted one-time are not add-ons
    assert "custom:3" not in by_source
    assert "one_time:99" not in by_source

    # prices
    price32 = (
        db_session.query(AddOnPrice).filter_by(add_on_id=ip32.id, is_active=True).one()
    )
    assert price32.price_type == PriceType.recurring
    assert Decimal(str(price32.amount)) == Decimal("2687.50")
    install_price = (
        db_session.query(AddOnPrice)
        .filter_by(add_on_id=by_source["one_time:1"].id, is_active=True)
        .one()
    )
    assert install_price.price_type == PriceType.one_time


def test_import_is_idempotent(db_session):
    import_addon_rows(db_session, _ONE_TIME, _CUSTOM)
    first_count = db_session.query(AddOn).count()
    price_first = db_session.query(AddOnPrice).count()

    # re-run with an updated price — updates in place, no duplicates
    bumped = [dict(r) for r in _CUSTOM]
    bumped[0]["price"] = Decimal("3000.00")
    import_addon_rows(db_session, _ONE_TIME, bumped)

    assert db_session.query(AddOn).count() == first_count
    assert db_session.query(AddOnPrice).count() == price_first
    ip32 = db_session.query(AddOn).filter_by(splynx_source="custom:8").one()
    price = (
        db_session.query(AddOnPrice).filter_by(add_on_id=ip32.id, is_active=True).one()
    )
    assert Decimal(str(price.amount)) == Decimal("3000.00")


def _offer(db, name, code):
    o = CatalogOffer(
        name=name,
        code=code,
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=BillingMode.prepaid,
        is_active=True,
    )
    db.add(o)
    db.flush()
    return o


def _sub(db, subscriber, offer):
    s = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        start_at=datetime.now(UTC),
        next_billing_at=datetime.now(UTC),
    )
    db.add(s)
    db.flush()
    return s


def test_seed_ip_offer_links_targets_real_plans_only(db_session, subscriber):
    plan = _offer(db_session, "Unlimited 3", "unlimited-3")
    _sub(db_session, subscriber, plan)  # real plan in use
    ip_offer = _offer(db_session, "/29 IP", "ip-29")  # an IP-block offer itself
    _sub(db_session, subscriber, ip_offer)
    _offer(db_session, "E2E Test Plan", "e2e-test")  # active but no subscriber
    ip_addon = AddOn(
        name="/29 IP",
        addon_type=AddOnType.extra_ip,
        is_active=True,
        ip_is_public=True,
        ip_prefix_length=29,
        splynx_source="custom:test29",
    )
    db_session.add(ip_addon)
    db_session.commit()

    summary = seed_ip_addon_offer_links(db_session)
    assert summary["ip_addons"] == 1
    assert summary["links_created"] == 1

    links = db_session.query(OfferAddOn).all()
    assert len(links) == 1
    assert links[0].offer_id == plan.id  # only the real plan, not the IP/empty offers

    # idempotent
    assert seed_ip_addon_offer_links(db_session)["links_created"] == 0
