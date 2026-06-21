"""Customer-facing connection-status flapping debounce (#48b).

A bad live_status (degraded/outage) only surfaces to the customer once it has
persisted past the dwell window; good news and operator-declared incidents
surface immediately. This stops a single flapping Zabbix poll from showing a
customer a false outage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.topology.selfcare import customer_connection_status

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _fiber(db, subscriber, live_status, live_status_at):
    olt = OLTDevice(name="OLT-1", hostname="olt1", mgmt_ip="10.0.0.1")
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db.add_all([olt, pop])
    db.flush()
    db.add(
        NetworkDevice(
            name="olt1-node",
            matched_device_type="olt",
            matched_device_id=olt.id,
            pop_site_id=pop.id,
            zabbix_hostid="201",
            live_status=live_status,
            live_status_at=live_status_at,
        )
    )
    ont = OntUnit(serial_number="SN-1", olt_device_id=olt.id)
    db.add(ont)
    db.flush()
    db.add(OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True))
    db.flush()


def test_recent_down_is_suppressed_as_healthy(db_session, subscriber, subscription):
    # Just flipped down 60s ago — within the 360s dwell → don't cry outage.
    _fiber(db_session, subscriber, "down", NOW - timedelta(seconds=60))
    out = customer_connection_status(db_session, subscription, now=NOW)
    assert out["status"] == "healthy"


def test_settled_down_surfaces_as_outage(db_session, subscriber, subscription):
    # Down for 10 min — past dwell → real outage surfaces.
    _fiber(db_session, subscriber, "down", NOW - timedelta(seconds=600))
    out = customer_connection_status(db_session, subscription, now=NOW)
    assert out["status"] == "outage"


def test_recent_problem_is_suppressed(db_session, subscriber, subscription):
    _fiber(db_session, subscriber, "problem", NOW - timedelta(seconds=60))
    out = customer_connection_status(db_session, subscription, now=NOW)
    assert out["status"] == "healthy"


def test_up_is_healthy_immediately(db_session, subscriber, subscription):
    # Good news is never debounced.
    _fiber(db_session, subscriber, "up", NOW - timedelta(seconds=1))
    out = customer_connection_status(db_session, subscription, now=NOW)
    assert out["status"] == "healthy"


def test_missing_timestamp_suppresses_bad_state(db_session, subscriber, subscription):
    # No live_status_at → can't prove it settled → treat as transient (healthy).
    _fiber(db_session, subscriber, "down", None)
    out = customer_connection_status(db_session, subscription, now=NOW)
    assert out["status"] == "healthy"
