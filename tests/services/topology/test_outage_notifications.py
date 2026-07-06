"""Outage-notification foundation — outage classifier P4 (design §P4).

Covers: disabled/no-actor is a no-op; enabled dispatch emits + audits; persisted
debounce suppresses a repeat; opt-out is suppressed; the confidence gate rejects
low-confidence inferred boundaries; the per-run cap is enforced; and channel
selection is DELEGATED to the notification system (we emit an outage EventType,
never pick a channel). The actual send is always mocked — nothing is delivered.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit, OnuOnlineStatus
from app.models.network_monitoring import (
    DeviceRole,
    NetworkDevice,
    OutageNotificationDispatch,
)
from app.models.subscriber import Subscriber
from app.services.topology import outage_notifications
from app.services.topology.outage import declare_outage
from app.services.topology.outage_notifications import (
    dispatch_outage_notifications,
    plan_outage_notifications,
)

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
ACTOR = uuid.uuid4()


def _enable(monkeypatch):
    monkeypatch.setattr(outage_notifications, "_enabled", lambda: True)


def _count(db, **filters):
    q = db.query(OutageNotificationDispatch)
    for k, v in filters.items():
        q = q.filter(getattr(OutageNotificationDispatch, k) == v)
    return q.count()


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


def _area_incident_with_customers(db, offer_id, n, *, opted_out_idx=()):
    """N customers behind ONE node with an operator-declared (open) incident —
    all share the same qualifying area boundary."""
    olt, node = _olt_with_node(db)
    subs = []
    for i in range(n):
        sub = _sub(db, offer_id, opted_out=(i in opted_out_idx))
        _down_ont(db, sub.subscriber_id, olt.id)
        subs.append(sub)
    declare_outage(db, node=node, declared_by="noc")
    return node, subs


# --- gating ----------------------------------------------------------------


def test_disabled_dispatch_is_noop(db_session, catalog_offer):
    _node, subs = _area_incident_with_customers(db_session, catalog_offer.id, 1)
    out = dispatch_outage_notifications(
        db_session, [s.id for s in subs], actor_id=ACTOR, now=NOW
    )
    assert out["dispatched"] is False
    assert out["reason"] == "disabled"
    assert _count(db_session) == 0  # no audit rows written


def test_no_actor_is_noop(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    _node, subs = _area_incident_with_customers(db_session, catalog_offer.id, 1)
    out = dispatch_outage_notifications(
        db_session, [s.id for s in subs], actor_id=None, now=NOW
    )
    assert out["dispatched"] is False
    assert out["reason"] == "no_actor"
    assert _count(db_session) == 0


# --- dispatch + audit + delegation -----------------------------------------


def test_dispatch_emits_and_audits(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    calls = []
    monkeypatch.setattr(
        outage_notifications,
        "_emit",
        lambda s, t, tgt, subj, actor: calls.append((t, tgt.subscriber_id)) or True,
    )
    _node, subs = _area_incident_with_customers(db_session, catalog_offer.id, 2)
    out = dispatch_outage_notifications(
        db_session, [s.id for s in subs], actor_id=ACTOR, now=NOW
    )
    assert out["dispatched"] is True
    assert out["sent_total"] == 2
    assert len(calls) == 2
    assert all(t == "outage_area" for t, _ in calls)  # area type, not a channel
    assert _count(db_session, status="sent") == 2
    assert db_session.query(OutageNotificationDispatch).first().category == "service"


def test_delegates_to_notification_system(db_session, catalog_offer, monkeypatch):
    """We emit an outage EventType with content; we never choose a channel."""
    _enable(monkeypatch)
    import app.services.events as events_pkg
    from app.services.events.types import EventType

    # Build the incident FIRST (declare_outage emits its own network.alert), then
    # start capturing so we only see the dispatch's emit.
    _node, subs = _area_incident_with_customers(db_session, catalog_offer.id, 1)
    seen = []
    monkeypatch.setattr(
        events_pkg,
        "emit_event",
        lambda db, et, payload, **kw: seen.append((et, payload, kw)),
    )
    dispatch_outage_notifications(
        db_session, [s.id for s in subs], actor_id=ACTOR, now=NOW
    )
    assert len(seen) == 1
    et, payload, kw = seen[0]
    assert et == EventType.outage_area
    assert "message" in payload and payload["subscriber_name"]
    assert "channel" not in payload and "channel" not in kw  # not our concern
    assert kw["subscriber_id"] == subs[0].subscriber_id


# --- persisted debounce ----------------------------------------------------


def test_persisted_debounce_suppresses_repeat(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(outage_notifications, "_emit", lambda *a, **k: True)
    _node, subs = _area_incident_with_customers(db_session, catalog_offer.id, 1)
    ids = [s.id for s in subs]
    first = dispatch_outage_notifications(db_session, ids, actor_id=ACTOR, now=NOW)
    assert first["sent_total"] == 1
    # A minute later the persisted 'sent' row debounces the whole boundary.
    again = dispatch_outage_notifications(
        db_session, ids, actor_id=ACTOR, now=NOW + timedelta(minutes=1)
    )
    assert again["sent_total"] == 0
    assert again["counts"]["skipped_debounce"] == 1


# --- opt-out ---------------------------------------------------------------


def test_optout_suppressed(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    calls = []
    monkeypatch.setattr(
        outage_notifications, "_emit", lambda *a, **k: calls.append(1) or True
    )
    _node, subs = _area_incident_with_customers(
        db_session, catalog_offer.id, 1, opted_out_idx=(0,)
    )
    out = dispatch_outage_notifications(
        db_session, [s.id for s in subs], actor_id=ACTOR, now=NOW
    )
    assert out["sent_total"] == 0
    assert out["counts"]["suppressed_optout"] == 1
    assert calls == []  # never emitted


# --- confidence gate -------------------------------------------------------


def test_confidence_gate(db_session, catalog_offer):
    from app.services.topology.outage import declare_outage as _declare
    from app.services.topology.outage_notifications import _area_boundary_qualifies

    # Operator-declared open incident -> trusted.
    _olt, node = _olt_with_node(db_session)
    inc = _declare(db_session, node=node, declared_by="noc")
    assert _area_boundary_qualifies(db_session, inc.id, NOW) is True
    # A healthy node id (not node_outage) -> does not qualify.
    _olt2, up_node = _olt_with_node(db_session, live_status="up")
    assert _area_boundary_qualifies(db_session, up_node.id, NOW) is False


# --- cap -------------------------------------------------------------------


def test_cap_enforced_with_audit(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(outage_notifications, "_max_per_run", lambda: 1)
    monkeypatch.setattr(outage_notifications, "_emit", lambda *a, **k: True)
    _node, subs = _area_incident_with_customers(db_session, catalog_offer.id, 2)
    out = dispatch_outage_notifications(
        db_session, [s.id for s in subs], actor_id=ACTOR, now=NOW
    )
    assert out["sent_total"] == 1
    assert out["counts"]["skipped_cap"] == 1  # remainder audited, not dropped


# --- preview ---------------------------------------------------------------


def test_plan_preview_writes_nothing(db_session, catalog_offer, monkeypatch):
    _enable(monkeypatch)
    _node, subs = _area_incident_with_customers(db_session, catalog_offer.id, 1)
    plan = plan_outage_notifications(db_session, [s.id for s in subs], now=NOW)
    assert plan["dispatched"] is False
    assert plan["would_send_total"] == 1
    assert plan["area_outages"][0]["qualifies"] is True
    assert _count(db_session) == 0  # preview never audits/sends


def test_plan_disabled_would_send_zero(db_session, catalog_offer):
    _node, subs = _area_incident_with_customers(db_session, catalog_offer.id, 1)
    plan = plan_outage_notifications(db_session, [s.id for s in subs], now=NOW)
    assert plan["enabled"] is False
    assert plan["would_send_total"] == 0
