"""Gated outage-notification planner — outage classifier P4 (design §P4).

Covers: disabled is a no-op, dry-run plans without sending, the area-vs-
per-customer split, opt-out suppression, and boundary debounce.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit, OnuOnlineStatus
from app.models.network_monitoring import DeviceRole, NetworkDevice
from app.models.subscriber import Subscriber
from app.services.topology import outage_notifications
from app.services.topology.outage import declare_outage
from app.services.topology.outage_notifications import plan_outage_notifications

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _enable(monkeypatch):
    # Settings is a frozen dataclass; patch the gate function instead.
    monkeypatch.setattr(outage_notifications, "_enabled", lambda: True)


def _sub(db, offer_id, *, opted_out=False):
    meta = {"service_notifications": False} if opted_out else None
    s = Subscriber(
        first_name="A",
        last_name="B",
        email=f"{uuid.uuid4().hex}@ex.com",
        metadata_=meta,
    )
    db.add(s)
    db.flush()
    sub = Subscription(
        subscriber_id=s.id, offer_id=offer_id, status=SubscriptionStatus.active
    )
    db.add(sub)
    db.flush()
    return sub


def _olt_with_node(db, *, live_status="up"):
    olt = OLTDevice(name=f"olt-{uuid.uuid4().hex[:6]}")
    db.add(olt)
    db.flush()
    node = NetworkDevice(
        name=f"olt-node-{uuid.uuid4().hex[:6]}",
        matched_device_type="olt",
        matched_device_id=olt.id,
        role=DeviceRole.edge,
        is_active=True,
        live_status=live_status,
    )
    db.add(node)
    db.flush()
    return olt, node


def _down_ont(db, subscriber_id, olt_device_id):
    # ONT online + good Rx + stale ACS + no session => router_offline (down).
    ont = OntUnit(
        serial_number=uuid.uuid4().hex[:12],
        olt_status=OnuOnlineStatus.online,
        onu_rx_signal_dbm=-18.0,
        olt_device_id=olt_device_id,
        acs_last_inform_at=NOW - timedelta(hours=2),
    )
    db.add(ont)
    db.flush()
    db.add(OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber_id, active=True))
    db.flush()
    return ont


def _customer_under_area_outage(db, offer_id, *, opted_out=False):
    sub = _sub(db, offer_id, opted_out=opted_out)
    olt, node = _olt_with_node(db)
    _down_ont(db, sub.subscriber_id, olt.id)
    declare_outage(db, node=node, declared_by="noc")
    return sub


def _customer_last_mile_only(db, offer_id):
    sub = _sub(db, offer_id)
    olt, _node = _olt_with_node(db)
    _down_ont(db, sub.subscriber_id, olt.id)  # router_offline, no incident
    return sub


# --- gating ----------------------------------------------------------------


def test_disabled_is_noop(db_session, catalog_offer):
    sub = _customer_under_area_outage(db_session, catalog_offer.id)
    plan = plan_outage_notifications(db_session, [sub.id], now=NOW, debounce_state={})
    assert plan["enabled"] is False
    assert plan["would_send_total"] == 0
    assert plan["dispatched"] is False


def test_dry_run_plans_without_dispatch(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    sub = _customer_under_area_outage(db_session, catalog_offer.id)
    plan = plan_outage_notifications(
        db_session, [sub.id], now=NOW, dry_run=True, debounce_state={}
    )
    assert plan["enabled"] is True
    assert plan["dispatched"] is False  # never sends, even enabled
    assert plan["would_send_total"] == 1
    assert len(plan["area_outages"]) == 1
    assert plan["area_outages"][0]["recipients"] == 1


# --- the split -------------------------------------------------------------


def test_area_vs_per_customer_split(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    area_sub = _customer_under_area_outage(db_session, catalog_offer.id)
    lm_sub = _customer_last_mile_only(db_session, catalog_offer.id)
    plan = plan_outage_notifications(
        db_session, [area_sub.id, lm_sub.id], now=NOW, debounce_state={}
    )
    assert len(plan["area_outages"]) == 1
    assert plan["area_outages"][0]["recipients"] == 1
    # the last-mile-only customer is in per_customer, not area.
    assert len(plan["per_customer"]) == 1
    assert plan["per_customer"][0]["recipients"] == 1
    assert "area" in plan["area_outages"][0]["sample_body"].lower()
    assert "area" not in plan["per_customer"][0]["sample_body"].lower()


# --- opt-out ---------------------------------------------------------------


def test_optout_suppressed_not_counted(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    sub = _customer_under_area_outage(db_session, catalog_offer.id, opted_out=True)
    plan = plan_outage_notifications(db_session, [sub.id], now=NOW, debounce_state={})
    assert plan["would_send_total"] == 0
    assert plan["area_outages"][0]["recipients"] == 0
    assert plan["area_outages"][0]["suppressed_optout"] == 1


# --- debounce --------------------------------------------------------------


def test_debounce_suppresses_repeat(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    sub = _customer_under_area_outage(db_session, catalog_offer.id)
    state: dict = {}
    first = plan_outage_notifications(
        db_session, [sub.id], now=NOW, debounce_state=state
    )
    assert first["would_send_total"] == 1
    assert first["area_outages"][0]["debounced"] is False
    # same boundary again a minute later -> debounced, not re-sent.
    again = plan_outage_notifications(
        db_session, [sub.id], now=NOW + timedelta(minutes=1), debounce_state=state
    )
    assert again["area_outages"][0]["debounced"] is True
    assert again["would_send_total"] == 0
