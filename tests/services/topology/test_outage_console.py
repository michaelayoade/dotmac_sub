"""Outage console read-model — P4 surface (design §P4).

Exercises the classifier-driven aggregator over P1/P2/P3: the health summary
counts across mixed node states, active_outages surfacing a localized boundary
while separating the monitoring-fault self-heal queue, and outage_detail
returning per-customer last-mile verdicts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import DeviceRole, NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.topology.outage_console import (
    active_outages,
    network_health_summary,
    outage_detail,
)

# --- helpers (mirror test_affected / test_health_classifier) ---------------


def _nas_node(db, name, live_status, role=DeviceRole.edge):
    nas = NasDevice(
        name=f"{name}-nas", management_ip=f"10.9.{uuid.uuid4().int % 250}.1"
    )
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name=name,
        matched_device_type="nas",
        matched_device_id=nas.id,
        role=role,
        is_active=True,
        ping_enabled=True,
        live_status=live_status,
    )
    db.add(node)
    db.flush()
    return node, nas


def _sub(db, offer_id, nas_id):
    s = Subscriber(first_name="A", last_name="B", email=f"{uuid.uuid4().hex}@ex.com")
    db.add(s)
    db.flush()
    sub = Subscription(
        subscriber_id=s.id,
        offer_id=offer_id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=nas_id,
    )
    db.add(sub)
    db.flush()
    return sub


def _session(db, sub, nas_id, *, age=timedelta(0)):
    ts = datetime.now(UTC) - age
    db.add(
        RadiusActiveSession(
            subscriber_id=sub.subscriber_id,
            subscription_id=sub.id,
            nas_device_id=nas_id,
            username="u",
            acct_session_id=uuid.uuid4().hex,
            session_start=ts,
            last_update=ts,
        )
    )
    db.flush()


# --- network_health_summary: counts across the four states -----------------


def test_health_summary_counts_mixed_states(db_session, catalog_offer):
    # healthy: mgmt up + a live session behind it.
    n_h, nas_h = _nas_node(db_session, "healthy", "up")
    _session(db_session, _sub(db_session, catalog_offer.id, nas_h.id), nas_h.id)
    # service_fault: mgmt up, had a customer, none online now.
    n_sf, nas_sf = _nas_node(db_session, "svcfault", "up")
    _sub(db_session, catalog_offer.id, nas_sf.id)
    # node_outage: mgmt down, had a customer, none online.
    n_out, nas_out = _nas_node(db_session, "outage", "down")
    _sub(db_session, catalog_offer.id, nas_out.id)
    # monitoring_fault: mgmt down, but a live session proves it up (impossible).
    n_mf, nas_mf = _nas_node(db_session, "monfault", "down")
    _session(db_session, _sub(db_session, catalog_offer.id, nas_mf.id), nas_mf.id)

    summary = network_health_summary(db_session)
    c = summary["counts"]
    assert c["healthy"] == 1
    assert c["service_fault"] == 1
    assert c["node_outage"] == 1
    assert c["monitoring_fault"] == 1
    assert summary["total_nodes"] == 4
    # not_healthy excludes the healthy node; worst (outage) sorts first.
    classes = [b["class"] for b in summary["not_healthy"]]
    assert "healthy" not in classes
    assert classes[0] == "node_outage"


def test_health_summary_stale_session_is_not_proof_of_life(db_session, catalog_offer):
    # A session older than the freshness window doesn't count as online, so an
    # mgmt-up node with only stale sessions is a service_fault, not healthy.
    node, nas = _nas_node(db_session, "stale", "up")
    _session(
        db_session,
        _sub(db_session, catalog_offer.id, nas.id),
        nas.id,
        age=timedelta(hours=3),
    )
    summary = network_health_summary(db_session)
    assert summary["counts"]["service_fault"] == 1
    assert summary["counts"]["healthy"] == 0


# --- active_outages: boundary vs self-heal queue ---------------------------


def test_active_outages_surfaces_boundary_and_separates_monitoring_fault(
    db_session, catalog_offer
):
    n_out, nas_out = _nas_node(db_session, "outage", "down")
    _sub(db_session, catalog_offer.id, nas_out.id)
    n_mf, nas_mf = _nas_node(db_session, "monfault", "down")
    _session(db_session, _sub(db_session, catalog_offer.id, nas_mf.id), nas_mf.id)

    res = active_outages(db_session)
    outage_nodes = {o["failure_node"] for o in res["outages"]}
    mf_nodes = {m["node_id"] for m in res["monitoring_faults"]}

    # The real outage localizes to a boundary and appears in outages.
    assert n_out.id in outage_nodes
    # The impossible contradiction is a self-heal item, NOT an outage.
    assert n_mf.id in mf_nodes
    assert n_mf.id not in outage_nodes


def test_active_outages_empty_when_all_healthy(db_session, catalog_offer):
    node, nas = _nas_node(db_session, "healthy", "up")
    _session(db_session, _sub(db_session, catalog_offer.id, nas.id), nas.id)
    res = active_outages(db_session)
    assert res["outages"] == []
    assert res["monitoring_faults"] == []


# --- outage_detail: per-customer verdicts ----------------------------------


def test_outage_detail_returns_per_customer_verdicts(db_session, catalog_offer):
    node, nas = _nas_node(db_session, "outage", "down")
    _sub(db_session, catalog_offer.id, nas.id)

    detail = outage_detail(db_session, node.id)
    assert detail is not None
    assert detail["class"] == "node_outage"
    assert detail["count"] == 1
    assert detail["node"]["medium"] == "network"
    assert len(detail["customers"]) == 1
    cust = detail["customers"][0]
    # NAS-only customer under a shared node outage should display as an area
    # outage, while preserving the raw P2 evidence that no ONT/radio exists.
    assert cust["online"] is False
    assert cust["verdict"] == "area_outage"
    assert cust["medium"] == "network"
    assert cust["customer_message"] == "Affected by shared network outage at outage."
    assert cust["agent_action"] == "network_team_restore_node - restore outage"
    assert cust["evidence"]["raw_verdict"] == "unknown"
    assert cust["evidence"]["access_device_kind"] == "nas"
    # No OLT match -> no predictive branch alerts.
    assert detail["predictive"]["co_failure"] == []
    assert detail["predictive"]["rx_droop"] == []


def test_outage_detail_unknown_node_returns_none(db_session):
    assert outage_detail(db_session, uuid.uuid4()) is None
