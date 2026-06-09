"""Reclassification of IP-block subscriptions into add-ons (dry-run + apply)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.catalog import (
    AccessType,
    AddOn,
    AddOnType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.migrations.reclassify_ip_offers import (
    apply_reclassification,
    build_reclassification_plan,
)


def _subscriber(db, email):
    s = Subscriber(first_name="T", last_name="User", email=email)
    db.add(s)
    db.flush()
    return s


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


def _sub(db, subscriber, offer, *, ipv4=None, login=None):
    s = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=offer.billing_mode,
        start_at=datetime.now(UTC),
        next_billing_at=datetime.now(UTC),
        ipv4_address=ipv4,
        login=login,
    )
    db.add(s)
    db.flush()
    return s


def _ip_addon(db, prefix):
    a = AddOn(
        name=f"/{prefix} IP",
        addon_type=AddOnType.extra_ip,
        is_active=True,
        ip_is_public=True,
        ip_prefix_length=prefix,
        splynx_source=f"custom:t{prefix}",
    )
    db.add(a)
    db.flush()
    return a


def _scenario(db):
    plan_offer = _offer(db, "Unlimited 3", "unlimited-3")
    ip29 = _offer(db, "/29 IP", "ip-29")
    ip30 = _offer(db, "/30 IP", "ip-30")
    _ip_addon(db, 29)  # only a /29 add-on exists

    # A: has a main plan + a /29 IP block (with a real IP) → reclassifiable
    a = _subscriber(db, "a@x.io")
    main_a = _sub(db, a, plan_offer)
    _sub(db, a, ip29, ipv4="102.0.0.8")
    # B: a /29 IP block but NO main plan → skip
    b = _subscriber(db, "b@x.io")
    _sub(db, b, ip29)
    # C: a /30 IP block but no /30 add-on exists → skip
    c = _subscriber(db, "c@x.io")
    _sub(db, c, plan_offer)
    _sub(db, c, ip30)
    # D: a /29 IP block that carries its OWN RADIUS login → safety-blocked
    d = _subscriber(db, "d@x.io")
    _sub(db, d, plan_offer)
    _sub(db, d, ip29, login="100099999")
    db.commit()
    return main_a


def test_plan_classifies_each_ip_subscription(db_session):
    _scenario(db_session)
    plan = build_reclassification_plan(db_session)
    s = plan["summary"]
    assert s["ip_subscriptions"] == 4
    assert s["would_reclassify"] == 1
    assert s["vestigial_ipv4"] == 1  # A's /29 carries a (non-RADIUS) ipv4
    assert s["radius_login_blocked"] == 1  # D's /29 has a RADIUS login
    assert s["skip_reasons"].get("no_main_subscription") == 1
    assert s["skip_reasons"].get("no_matching_addon") == 1
    assert s["skip_reasons"].get("has_radius_login") == 1


def test_apply_attaches_addon_and_archives(db_session):
    main_a = _scenario(db_session)
    result = apply_reclassification(db_session, commit=True)
    assert result["applied"] == 1

    # the main plan gained the /29 add-on
    links = (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == main_a.id)
        .all()
    )
    assert len(links) == 1
    add_on = db_session.get(AddOn, links[0].add_on_id)
    assert add_on.ip_prefix_length == 29

    # the standalone IP subscription was archived
    ip_sub = (
        db_session.query(Subscription)
        .filter(Subscription.status == SubscriptionStatus.archived)
        .one()
    )
    assert ip_sub.end_at is not None
    assert ip_sub.cancel_reason.startswith("reclassified_to_addon:")

    # idempotent — a second run finds nothing active to move
    again = apply_reclassification(db_session, commit=True)
    assert again["applied"] == 0
    assert (
        db_session.query(SubscriptionAddOn)
        .filter(SubscriptionAddOn.subscription_id == main_a.id)
        .count()
        == 1
    )
