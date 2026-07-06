"""Outage classifier P1 — proof-of-life + mgmt/data-plane node state.

Design: docs/designs/OUTAGE_CLASSIFIER.md §1 (node ladder), §2 (proof-of-life),
§3 (localization), §7.1/§7.4 (small-N, failover edge cases).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    NetworkTopologyLink,
)
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.topology.affected import affected_customers
from app.services.topology.health_classifier import (
    HEALTHY,
    MONITORING_FAULT,
    NODE_OUTAGE,
    SERVICE_FAULT,
    UNKNOWN,
    classify_node,
    localize_outage,
    online_subscribers,
    online_subscription_ids,
)

# --- helpers (mirror test_affected) ---------------------------------------


def _node(db, name, mtype=None, mid=None, role=DeviceRole.edge, live_status=None):
    n = NetworkDevice(
        name=name,
        matched_device_type=mtype,
        matched_device_id=mid,
        role=role,
        is_active=True,
        live_status=live_status,
    )
    db.add(n)
    db.flush()
    return n


def _sub(db, offer_id, nas_id=None, status=SubscriptionStatus.active):
    s = Subscriber(first_name="A", last_name="B", email=f"{uuid.uuid4().hex}@ex.com")
    db.add(s)
    db.flush()
    sub = Subscription(
        subscriber_id=s.id,
        offer_id=offer_id,
        status=status,
        provisioning_nas_device_id=nas_id,
    )
    db.add(sub)
    db.flush()
    return sub


def _session(db, subscription, nas_device_id, *, age=timedelta(0)):
    ts = datetime.now(UTC) - age
    ras = RadiusActiveSession(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        nas_device_id=nas_device_id,
        username="u",
        acct_session_id=uuid.uuid4().hex,
        session_start=ts,
        last_update=ts,
    )
    db.add(ras)
    db.flush()
    return ras


def _link(db, a, b):
    db.add(
        NetworkTopologyLink(
            source_device_id=a.id,
            target_device_id=b.id,
            source="lldp_neighbor",
            is_active=True,
        )
    )
    db.flush()


# --- classify_node: the four states + unknown (design §1) ------------------


def _stub(live_status):
    return NetworkDevice(name="x", live_status=live_status)


def test_classify_healthy_mgmt_up_and_online():
    assert classify_node(_stub("up"), online_count=5, had_prior_life=True) == HEALTHY


def test_classify_service_fault_mgmt_up_zero_online_prior_life():
    # up/up/down row: reachable, serving nobody it used to -> data-plane, not area.
    assert (
        classify_node(_stub("up"), online_count=0, had_prior_life=True) == SERVICE_FAULT
    )


def test_classify_node_outage_mgmt_down_zero_online_prior_life():
    assert (
        classify_node(_stub("down"), online_count=0, had_prior_life=True) == NODE_OUTAGE
    )


def test_classify_monitoring_fault_session_up_but_mgmt_down_is_impossible():
    # session up + ping/snmp down is physically impossible -> the check lies.
    assert (
        classify_node(_stub("down"), online_count=3, had_prior_life=True)
        == MONITORING_FAULT
    )
    # unknown/unwarmed mgmt but customers online -> still a monitoring gap.
    assert (
        classify_node(_stub(None), online_count=1, had_prior_life=True)
        == MONITORING_FAULT
    )


def test_classify_unknown_no_prior_life_or_single_dark_signal():
    # never had life -> dormant / small-N / unprovisioned, nothing to conclude.
    assert classify_node(_stub("down"), online_count=0, had_prior_life=False) == UNKNOWN
    # mgmt unknown + zero online + prior life -> only one dark signal, not enough.
    assert (
        classify_node(_stub("unknown"), online_count=0, had_prior_life=True) == UNKNOWN
    )
    assert (
        classify_node(_stub("problem"), online_count=0, had_prior_life=True) == UNKNOWN
    )


# --- proof-of-life freshness (design §2 / §7.6) ---------------------------


def test_online_subscription_ids_only_counts_fresh_sessions(db_session, catalog_offer):
    nas = NasDevice(name="NAS-F", management_ip="10.0.9.1")
    db_session.add(nas)
    db_session.flush()
    fresh = _sub(db_session, catalog_offer.id, nas_id=nas.id)
    stale = _sub(db_session, catalog_offer.id, nas_id=nas.id)
    never = _sub(db_session, catalog_offer.id, nas_id=nas.id)
    _session(db_session, fresh, nas.id, age=timedelta(minutes=2))
    _session(db_session, stale, nas.id, age=timedelta(hours=3))  # older than TTL

    ids = online_subscription_ids(db_session, [fresh.id, stale.id, never.id])
    assert ids == {fresh.id}


def test_online_subscription_ids_empty_input():
    # pure guard — no DB round trip needed.
    assert online_subscription_ids(None, []) == set()


# --- affected_customers overlay (additive) --------------------------------


def test_affected_customers_exposes_online_overlay(db_session, catalog_offer):
    nas = NasDevice(name="NAS-O", management_ip="10.0.9.2")
    db_session.add(nas)
    db_session.flush()
    node = _node(db_session, "nas-node", "nas", nas.id, live_status="up")
    online = _sub(db_session, catalog_offer.id, nas_id=nas.id)
    offline = _sub(db_session, catalog_offer.id, nas_id=nas.id)
    _session(db_session, online, nas.id)

    out = affected_customers(db_session, node=node)
    # existing keys preserved
    assert {online.id, offline.id} == {s.id for s in out["subscriptions"]}
    assert out["count"] == 2
    # additive overlay
    assert out["online_count"] == 1
    assert out["online_by_node"][node.id] == 1


# --- localization (design §3) ---------------------------------------------


def test_localize_finds_deepest_dark_node_under_a_live_peer(db_session, catalog_offer):
    # core -> edge_up (has a live customer) ; core -> edge_dark (all dark).
    core = _node(db_session, "core", role=DeviceRole.core, live_status="up")
    up_nas = NasDevice(name="NAS-UP", management_ip="10.0.9.3")
    dark_nas = NasDevice(name="NAS-DK", management_ip="10.0.9.4")
    db_session.add_all([up_nas, dark_nas])
    db_session.flush()
    edge_up = _node(db_session, "edge-up", "nas", up_nas.id, live_status="up")
    edge_dark = _node(db_session, "edge-dark", "nas", dark_nas.id, live_status="down")
    _link(db_session, core, edge_up)
    _link(db_session, core, edge_dark)

    live = _sub(db_session, catalog_offer.id, nas_id=up_nas.id)
    _session(db_session, live, up_nas.id)  # survivor on edge_up
    for _ in range(4):  # edge_dark had customers, all offline now
        _sub(db_session, catalog_offer.id, nas_id=dark_nas.id)

    res = localize_outage(db_session, [core.id, edge_up.id, edge_dark.id])
    assert res is not None
    assert res["failure_node"] == edge_dark.id
    assert res["class"] == NODE_OUTAGE
    assert res["affected_online_before"] == 4
    assert res["affected_now"] == 0
    assert res["confidence"] == "high"  # live peer proves upstream up + N>=3


def test_localize_returns_none_when_every_node_has_a_survivor(
    db_session, catalog_offer
):
    nas = NasDevice(name="NAS-A", management_ip="10.0.9.5")
    db_session.add(nas)
    db_session.flush()
    node = _node(db_session, "alive", "nas", nas.id, live_status="up")
    live = _sub(db_session, catalog_offer.id, nas_id=nas.id)
    _session(db_session, live, nas.id)
    assert localize_outage(db_session, [node.id]) is None


def test_localize_small_n_lowers_confidence(db_session, catalog_offer):
    # a dark node with 1 provisioned customer -> can't infer plant, low confidence.
    nas = NasDevice(name="NAS-SM", management_ip="10.0.9.6")
    db_session.add(nas)
    db_session.flush()
    node = _node(db_session, "small", "nas", nas.id, live_status="down")
    _sub(db_session, catalog_offer.id, nas_id=nas.id)  # 1 customer, offline
    res = localize_outage(db_session, [node.id])
    assert res is not None
    assert res["failure_node"] == node.id
    assert res["confidence"] == "low"


# --- failover / global proof-of-life (design §7.4) ------------------------


def test_online_subscribers_is_global_across_nodes(db_session, catalog_offer):
    # A subscriber provisioned on NAS-A but currently online on NAS-B (failover)
    # must still read as online GLOBALLY -> not counted as an outage victim.
    home = NasDevice(name="NAS-HOME", management_ip="10.0.9.7")
    away = NasDevice(name="NAS-AWAY", management_ip="10.0.9.8")
    db_session.add_all([home, away])
    db_session.flush()
    roamer = _sub(db_session, catalog_offer.id, nas_id=home.id)
    _session(db_session, roamer, away.id)  # session lives on the OTHER nas

    # global proof-of-life sees them online despite the home NAS being dark.
    assert online_subscribers(db_session, [roamer.subscriber_id]) == {
        roamer.subscriber_id
    }
