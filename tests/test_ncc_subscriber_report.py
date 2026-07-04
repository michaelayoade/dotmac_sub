from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services import ncc_subscriber_report as ncc


# ── unit helpers ────────────────────────────────────────────────────────────
def test_normalize_state_and_zone():
    assert ncc.normalize_state("Lagos") == "Lagos"
    assert ncc.normalize_state("Lagos State") == "Lagos"
    assert ncc.normalize_state("Abuja") == "Federal Capital Territory"
    assert ncc.normalize_state("FCT") == "Federal Capital Territory"
    assert ncc.normalize_state("rivers") == "Rivers"
    assert ncc.normalize_state("Nowhere") == "Unknown"
    assert ncc.normalize_state(None) == "Unknown"
    assert ncc.zone_for_state("Lagos") == "South West"
    assert ncc.zone_for_state("Federal Capital Territory") == "North Central"
    assert ncc.zone_for_state("Kano") == "North West"
    assert ncc.zone_for_state("Unknown") == "Unknown"


def test_speed_bands():
    assert ncc.speed_band(1) == "256kbps-<2Mbps"
    assert ncc.speed_band(0) == "256kbps-<2Mbps"
    assert ncc.speed_band(5) == "2Mbps-<10Mbps"
    assert ncc.speed_band(10) == "10Mbps+"
    assert ncc.speed_band(100) == "10Mbps+"
    assert ncc.speed_band(None) == "unknown"


# ── integration ─────────────────────────────────────────────────────────────
def _offer(db, *, access, speed, mode=BillingMode.prepaid) -> CatalogOffer:
    o = CatalogOffer(
        name=f"Plan {uuid.uuid4().hex[:5]}",
        code=f"P-{uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=access,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        billing_mode=mode,
        speed_download_mbps=speed,
        is_active=True,
    )
    db.add(o)
    db.commit()
    return o


def _subscriber(db, *, region, category=SubscriberCategory.residential) -> Subscriber:
    s = Subscriber(
        first_name="A",
        last_name="B",
        email=f"s-{uuid.uuid4().hex[:8]}@x.io",
        subscriber_number=f"S-{uuid.uuid4().hex[:6]}",
        region=region,
    )
    s.category = category
    db.add(s)
    db.commit()
    return s


def _subscription(
    db,
    sub,
    offer,
    *,
    status=SubscriptionStatus.active,
    mode=BillingMode.prepaid,
    start_at=None,
    end_at=None,
):
    row = Subscription(
        subscriber_id=sub.id,
        offer_id=offer.id,
        status=status,
        billing_mode=mode,
        start_at=start_at,
        end_at=end_at,
    )
    db.add(row)
    db.commit()
    return row


def test_report_aggregates_active_subscriptions(db_session):
    fibre_fast = _offer(
        db_session, access=AccessType.fiber, speed=100, mode=BillingMode.postpaid
    )
    wireless_slow = _offer(db_session, access=AccessType.fixed_wireless, speed=5)

    corp = _subscriber(db_session, region="Lagos", category=SubscriberCategory.business)
    indiv = _subscriber(
        db_session, region="Abuja", category=SubscriberCategory.residential
    )

    _subscription(
        db_session, corp, fibre_fast, mode=BillingMode.postpaid
    )  # corporate/wired/postpaid/10M+
    _subscription(
        db_session, indiv, wireless_slow, mode=BillingMode.prepaid
    )  # individual/wireless/prepaid/2-10M
    # excluded — not active
    _subscription(db_session, indiv, fibre_fast, status=SubscriptionStatus.expired)

    r = ncc.build_ncc_subscriber_report(
        db_session,
        ncc.NccSubscriberReportParams(
            capacity={"points_of_presence": 28, "data_usage_tb": Decimal("2760.96")}
        ),
    )

    assert r["parameters"]["active_statuses"] == ["active"]
    assert r["total_active_subscriptions"] == 2
    assert r["by_connection"] == {"wired": 1, "wireless": 1}
    assert r["by_customer_type"] == {"corporate": 1, "individual": 1}
    assert r["by_billing_mode"] == {"postpaid": 1, "prepaid": 1}
    assert r["by_speed_band"]["10Mbps+"] == 1
    assert r["by_speed_band"]["2Mbps-<10Mbps"] == 1
    assert r["subscription_matrix"]["corporate"]["wired"] == 1
    assert r["subscription_matrix"]["individual"]["wireless"] == 1
    assert r["by_state"] == {"Federal Capital Territory": 1, "Lagos": 1}
    assert r["by_region"] == {"North Central": 1, "South West": 1}
    assert r["network_capacity"]["points_of_presence"] == 28


def test_report_empty(db_session):
    r = ncc.build_ncc_subscriber_report(db_session)
    assert r["total_active_subscriptions"] == 0
    assert r["by_state"] == {}
    assert r["network_capacity"]["points_of_presence"] is None


def test_point_in_time_and_status_params(db_session):
    from datetime import UTC, datetime

    offer = _offer(db_session, access=AccessType.fiber, speed=50)
    sub = _subscriber(db_session, region="Kano")

    q1_end = datetime(2026, 3, 31, tzinfo=UTC)
    # Started before the period end → counts at q1_end.
    _subscription(db_session, sub, offer, start_at=datetime(2026, 1, 10, tzinfo=UTC))
    # Ended before the period end → excluded at q1_end.
    _subscription(
        db_session,
        sub,
        offer,
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    # Starts after the period end → excluded at q1_end.
    _subscription(db_session, sub, offer, start_at=datetime(2026, 6, 1, tzinfo=UTC))
    # Suspended → excluded by default, included when status widened.
    _subscription(db_session, sub, offer, status=SubscriptionStatus.suspended)

    base = ncc.build_ncc_subscriber_report(
        db_session, ncc.NccSubscriberReportParams(as_of=q1_end)
    )
    assert base["total_active_subscriptions"] == 1  # only the still-running one

    widened = ncc.build_ncc_subscriber_report(
        db_session,
        ncc.NccSubscriberReportParams(
            as_of=q1_end,
            active_statuses=(SubscriptionStatus.active, SubscriptionStatus.suspended),
        ),
    )
    assert widened["total_active_subscriptions"] == 2  # + the suspended one
    assert set(widened["parameters"]["active_statuses"]) == {"active", "suspended"}


def test_parse_report_params():
    p = ncc.parse_report_params(
        as_of="2026-03-31",
        statuses="active,suspended,bogus",
        reseller_id="not-a-uuid",
        capacity={
            "points_of_presence": "28",
            "data_usage_tb": "",
            "access_capacity_gbps": None,
        },
    )
    assert p.as_of is not None and p.as_of.year == 2026 and p.as_of.hour == 23
    assert p.active_statuses == (
        SubscriptionStatus.active,
        SubscriptionStatus.suspended,
    )
    assert p.reseller_id is None
    assert p.capacity == {"points_of_presence": "28"}  # blanks/None dropped

    # empty statuses -> default active; blank as_of -> None (now)
    d = ncc.parse_report_params(as_of="", statuses="")
    assert d.active_statuses == (SubscriptionStatus.active,)
    assert d.as_of is None


def test_build_csv_layout(db_session):
    report = ncc.build_ncc_subscriber_report(db_session)  # empty report
    csv_text = ncc.build_ncc_subscriber_csv(report)
    assert "Total Active Internet Subscriptions,0" in csv_text
    assert "Corporate — Wired,0" in csv_text
    assert "Data Usage (TB) [manual]," in csv_text
    assert "Active Subscriptions per State" in csv_text
