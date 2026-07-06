"""Customer connection-status surface — outage classifier P4 (design §P4/§5).

Covers the per-customer verdict projection and — the point of the surface — the
area-vs-last-mile split: an area outage suppresses the "reboot your router"
blame and never leaks internal topology to the customer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
)
from app.models.network_monitoring import DeviceRole, NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.topology import last_mile
from app.services.topology.connection_status import (
    STATE_CONNECTED,
    STATE_OUTAGE,
    STATE_TROUBLE,
    assess,
    connection_status,
)
from app.services.topology.outage import declare_outage

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)

_SAFE_KEYS = {
    "state",
    "headline",
    "message",
    "advice",
    "medium",
    "area_outage",
    "checked_at",
}


# --- builders -------------------------------------------------------------


def _sub(db, offer_id):
    s = Subscriber(first_name="A", last_name="B", email=f"{uuid.uuid4().hex}@ex.com")
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


def _ont(
    db,
    subscriber_id,
    *,
    olt_device_id,
    status=OnuOnlineStatus.online,
    rx=-18.0,
    acs_age=timedelta(minutes=1),
):
    ont = OntUnit(
        serial_number=uuid.uuid4().hex[:12],
        olt_status=status,
        onu_rx_signal_dbm=rx,
        olt_device_id=olt_device_id,
        acs_last_inform_at=(NOW - acs_age) if acs_age is not None else None,
    )
    db.add(ont)
    db.flush()
    db.add(OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber_id, active=True))
    db.flush()
    return ont


def _live_session(db, sub, *, now=NOW):
    db.add(
        RadiusActiveSession(
            subscriber_id=sub.subscriber_id,
            subscription_id=sub.id,
            username="u",
            acct_session_id=uuid.uuid4().hex,
            session_start=now,
            last_update=now,
        )
    )
    db.flush()


# --- per-customer projection ----------------------------------------------


def test_connected_when_session_live(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    _live_session(db_session, sub)
    out = connection_status(db_session, sub, now=NOW)
    assert out["state"] == STATE_CONNECTED
    assert out["area_outage"] is False
    assert out["advice"] is None
    assert set(out) == _SAFE_KEYS


def test_router_offline_gives_reboot_advice(db_session, catalog_offer):
    # fiber ONT online, good Rx, ACS stale, no live session -> router_offline.
    sub = _sub(db_session, catalog_offer.id)
    olt, _node = _olt_with_node(db_session)
    _ont(
        db_session, sub.subscriber_id, olt_device_id=olt.id, acs_age=timedelta(hours=2)
    )
    out = connection_status(db_session, sub, now=NOW)
    a = assess(db_session, sub, now=NOW)
    assert a.verdict == last_mile.ROUTER_OFFLINE
    assert out["state"] == STATE_TROUBLE
    assert out["area_outage"] is False
    assert "off" in out["advice"].lower()  # reboot instruction present


def test_power_verdict_when_ont_absent_plant_up(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    olt, _node = _olt_with_node(db_session, live_status="up")
    _ont(
        db_session,
        sub.subscriber_id,
        olt_device_id=olt.id,
        status=OnuOnlineStatus.offline,
    )
    out = connection_status(db_session, sub, now=NOW)
    assert out["state"] == STATE_TROUBLE
    assert out["area_outage"] is False


# --- the area-vs-last-mile split (design §5/§7.3) -------------------------


def test_area_outage_suppresses_last_mile_blame(db_session, catalog_offer):
    """A declared area outage overrides the router_offline blame."""
    sub = _sub(db_session, catalog_offer.id)
    olt, node = _olt_with_node(db_session)
    # This customer would otherwise diagnose as router_offline ("reboot").
    _ont(
        db_session, sub.subscriber_id, olt_device_id=olt.id, acs_age=timedelta(hours=2)
    )
    # Operator declares an outage on the customer's access node.
    declare_outage(db_session, node=node, declared_by="noc")

    a = assess(db_session, sub, now=NOW)
    assert a.verdict == last_mile.ROUTER_OFFLINE  # underlying last-mile unchanged
    assert a.is_area_outage is True

    out = connection_status(db_session, sub, now=NOW)
    assert out["state"] == STATE_OUTAGE
    assert out["area_outage"] is True
    # blame suppressed: no "reboot" advice, message is the area message.
    assert out["advice"] is None
    assert "area" in out["message"].lower()


def test_area_outage_via_inference_when_node_dark(db_session, catalog_offer):
    """No declared incident, but the access node is fully dark for >=3 -> area."""
    _olt = OLTDevice(name=f"olt-{uuid.uuid4().hex[:6]}")
    db_session.add(_olt)
    db_session.flush()
    node = NetworkDevice(
        name=f"olt-node-{uuid.uuid4().hex[:6]}",
        matched_device_type="olt",
        matched_device_id=_olt.id,
        role=DeviceRole.edge,
        is_active=True,
        live_status="down",
    )
    db_session.add(node)
    db_session.flush()
    subs = []
    for _ in range(3):
        s = _sub(db_session, catalog_offer.id)
        _ont(
            db_session,
            s.subscriber_id,
            olt_device_id=_olt.id,
            status=OnuOnlineStatus.offline,
        )
        subs.append(s)
    out = connection_status(db_session, subs[0], now=NOW)
    assert out["area_outage"] is True
    assert out["state"] == STATE_OUTAGE


# --- no internal leak -----------------------------------------------------


def test_payload_never_leaks_internals(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    olt, node = _olt_with_node(db_session)
    _ont(
        db_session, sub.subscriber_id, olt_device_id=olt.id, acs_age=timedelta(hours=2)
    )
    declare_outage(db_session, node=node, declared_by="noc")

    out = connection_status(db_session, sub, now=NOW)
    # exactly the safe key set — no node ids, signal values, or verdict.
    assert set(out) == _SAFE_KEYS
    blob = str(out).lower()
    assert str(node.id) not in blob
    assert "router_offline" not in blob  # internal verdict not projected
    assert "rx" not in blob and "dbm" not in blob

    # assess() DOES carry the internal boundary id — but connection_status hides it.
    a = assess(db_session, sub, now=NOW)
    assert a.area_boundary_id is not None
    assert "area_boundary_id" not in out


# --- route: auth-scoped to the caller's own subscription ------------------


def test_status_json_requires_auth(db_session):
    import json
    from unittest.mock import patch

    from fastapi import Request

    from app.web.customer import connection as conn_web

    req = Request({"type": "http", "headers": [], "query_string": b""})
    with patch.object(conn_web, "get_current_customer_from_request", return_value=None):
        resp = conn_web.customer_connection_status_json(req, db=db_session)
    assert resp.status_code == 401
    assert json.loads(bytes(resp.body)) == {"detail": "Not authenticated"}


def test_status_json_returns_callers_own_status(db_session, catalog_offer):
    import json
    from unittest.mock import patch

    from fastapi import Request

    from app.web.customer import connection as conn_web

    sub = _sub(db_session, catalog_offer.id)
    _live_session(db_session, sub, now=datetime.now(UTC))
    req = Request({"type": "http", "headers": [], "query_string": b""})
    # The route only ever resolves THIS session's own subscription — a customer
    # can never address another's status.
    with (
        patch.object(
            conn_web,
            "get_current_customer_from_request",
            return_value={"subscriber_id": str(sub.subscriber_id)},
        ),
        patch.object(
            conn_web, "resolve_customer_subscription", return_value=sub
        ) as resolve,
    ):
        resp = conn_web.customer_connection_status_json(req, db=db_session)
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["state"] == STATE_CONNECTED
    assert set(body) == _SAFE_KEYS
    resolve.assert_called_once()
