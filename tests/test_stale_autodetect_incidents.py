"""Stale auto-detect incidents: customer-impact TTL + hygiene guardrail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.network_monitoring import NetworkDevice, OutageIncident
from app.services import admin_alerts, customer_service_state
from app.services.topology.outage import AUTO_DETECT_ACTOR


def _incident(db_session, *, status="open", declared_by=None, age_hours=0.0):
    node = NetworkDevice(
        name=f"ttl-node-{uuid4().hex[:8]}",
        source="zabbix_reconcile",
        is_active=True,
    )
    db_session.add(node)
    db_session.flush()
    incident = OutageIncident(
        root_node_id=node.id,
        status=status,
        detection_source="operator",
        declared_by=declared_by or "admin@dotmac",
        started_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )
    db_session.add(incident)
    db_session.commit()
    return incident


def _counted_incident_nodes(db_session, monkeypatch):
    """Run the outage-id helper; capture which incidents' scopes get resolved."""
    seen = []

    def _fake_affected(session, node=None, basestation=None, fdh=None):
        seen.append(node.id if node is not None else None)
        return {"subscriptions": []}

    monkeypatch.setattr(
        "app.services.topology.affected.affected_customers", _fake_affected
    )
    customer_service_state.active_outage_subscription_ids(db_session)
    return seen


def test_fresh_autodetect_incident_counts(db_session, monkeypatch):
    _incident(db_session, declared_by=AUTO_DETECT_ACTOR, age_hours=1)
    assert len(_counted_incident_nodes(db_session, monkeypatch)) == 1


def test_stale_autodetect_incident_is_excluded(db_session, monkeypatch):
    _incident(db_session, declared_by=AUTO_DETECT_ACTOR, age_hours=48)
    assert _counted_incident_nodes(db_session, monkeypatch) == []


def test_stale_manual_open_incident_still_counts(db_session, monkeypatch):
    # A human declared it; only a human resolves it — no TTL.
    _incident(db_session, declared_by="admin@dotmac", age_hours=48)
    assert len(_counted_incident_nodes(db_session, monkeypatch)) == 1


def test_stale_classifier_confirmed_still_counts(db_session, monkeypatch):
    # The classifier lifecycle debounces and auto-resolves itself; while it
    # says confirmed, it is trusted regardless of age.
    _incident(
        db_session,
        status="confirmed",
        declared_by="system:outage-classifier",
        age_hours=48,
    )
    assert len(_counted_incident_nodes(db_session, monkeypatch)) == 1


def test_guardrail_raises_on_stale_autodetect_rows(db_session):
    _incident(db_session, declared_by=AUTO_DETECT_ACTOR, age_hours=40)
    _incident(db_session, declared_by=AUTO_DETECT_ACTOR, age_hours=90)

    findings = admin_alerts._stale_autodetect_incident_findings(db_session)

    assert [f.fingerprint for f in findings] == [
        "infrastructure:outage:stale-autodetect"
    ]
    assert findings[0].severity.name == "critical"
    assert findings[0].details["count"] == 2


def test_guardrail_quiet_when_fresh_or_manual(db_session):
    _incident(db_session, declared_by=AUTO_DETECT_ACTOR, age_hours=2)
    _incident(db_session, declared_by="admin@dotmac", age_hours=90)

    assert admin_alerts._stale_autodetect_incident_findings(db_session) == []
