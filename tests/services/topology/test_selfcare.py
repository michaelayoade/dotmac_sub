"""Customer-safe connection status (Phase 3, P3.4)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.topology.selfcare import customer_connection_status


def _fiber(db_session, subscriber, live_status):
    olt = OLTDevice(name="OLT-1", hostname="olt1", mgmt_ip="10.0.0.1")
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add_all([olt, pop])
    db_session.flush()
    db_session.add(
        NetworkDevice(
            name="olt1-node",
            matched_device_type="olt",
            matched_device_id=olt.id,
            pop_site_id=pop.id,
            zabbix_hostid="201",
            live_status=live_status,
            live_status_at=datetime(2026, 6, 17, tzinfo=UTC),
        )
    )
    ont = OntUnit(serial_number="SN-1", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    db_session.flush()


def test_healthy(db_session, subscriber, subscription):
    _fiber(db_session, subscriber, "up")
    out = customer_connection_status(db_session, subscription)
    assert out == {"basestation": "Garki", "status": "healthy", "known_outage": False}


def test_outage_and_degraded(db_session, subscriber, subscription):
    _fiber(db_session, subscriber, "down")
    assert customer_connection_status(db_session, subscription)["status"] == "outage"


def test_unknown_when_no_path(db_session, subscription):
    out = customer_connection_status(db_session, subscription)
    assert out == {"basestation": None, "status": "unknown", "known_outage": False}


def test_no_internal_details_leak(db_session, subscriber, subscription):
    _fiber(db_session, subscriber, "problem")
    out = customer_connection_status(db_session, subscription)
    # Only the safe keys; no ip/device/node/gap internals.
    assert set(out.keys()) == {"basestation", "status", "known_outage"}
    assert out["status"] == "degraded"
    blob = repr(out).lower()
    for leaked in ("10.0.0.1", "olt1-node", "201", "matched", "node", "gap"):
        assert leaked not in blob


def test_connection_endpoint_registered():
    from app.api.me import router

    paths = {r.path for r in router.routes}
    assert "/me/subscriptions/{subscription_id}/connection" in paths
