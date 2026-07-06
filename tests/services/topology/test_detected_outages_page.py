"""Detected-outages console route + template wiring (P4 surface)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.topology.outage import (
    confirm_incident,
    discard_incident,
    open_classifier_incident,
    resolve_classifier_incident,
    start_clearing,
)


def _request(path: str = "/admin/network/detected-outages") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _node_with_bts(db):
    pop = PopSite(name=f"BTS-{uuid.uuid4().hex[:5]}", zabbix_group_id="1")
    db.add(pop)
    db.flush()
    node = NetworkDevice(
        name=f"node-{uuid.uuid4().hex[:5]}",
        matched_device_type="switch",
        pop_site_id=pop.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    return node, pop


def _render_context(db, monkeypatch):
    from app.web.admin import network_monitoring

    monkeypatch.setattr(
        network_monitoring, "_base_context", lambda *a, **k: {"request": _request()}
    )
    monkeypatch.setattr(
        network_monitoring.templates,
        "TemplateResponse",
        lambda template, context: {"template": template, "context": context},
    )
    resp = network_monitoring.detected_outages_console(_request(), db=db)
    return resp["context"]


def test_console_reads_persisted_confirmed_classifier_incident(db_session, monkeypatch):
    """The P4a console primary list is the PERSISTED debounced classifier
    incidents — a confirmed one renders with its state, source, node/basestation
    names and MTTR-so-far."""
    node, pop = _node_with_bts(db_session)
    confirmed_at = datetime.now(UTC) - timedelta(minutes=15)
    inc = open_classifier_incident(
        db_session,
        root_node=node,
        affected_count=7,
        confidence=0.88,
        classification="node_outage",
        now=confirmed_at,
    )
    confirm_incident(db_session, inc, now=confirmed_at)
    db_session.flush()

    context = _render_context(db_session, monkeypatch)
    rows = context["classifier_incidents"]
    row = next(r for r in rows if str(r["incident"].id) == str(inc.id))
    assert row["state"] == "confirmed"
    assert row["detection_source"] == "classifier"
    assert row["affected_count"] == 7
    assert row["confidence"] == 0.88
    assert row["classification"] == "node_outage"
    assert row["node_name"] == node.name
    assert row["basestation_name"] == pop.name
    assert row["confirmed_at"] is not None
    assert row["mttr_so_far_seconds"] is not None
    assert row["mttr_so_far_seconds"] >= 15 * 60 - 5


def test_console_excludes_discarded_and_resolved_from_active_view(
    db_session, monkeypatch
):
    """Terminal classifier incidents (discarded false positives, resolved) are
    NOT in the console's active list — only live suspected/confirmed/clearing."""
    node, _ = _node_with_bts(db_session)
    now = datetime.now(UTC)

    live = open_classifier_incident(db_session, root_node=node, now=now)
    confirm_incident(db_session, live, now=now)

    discarded = open_classifier_incident(db_session, root_node=node, now=now)
    discard_incident(db_session, discarded)

    resolved = open_classifier_incident(db_session, root_node=node, now=now)
    confirm_incident(db_session, resolved, now=now)
    start_clearing(db_session, resolved, now=now)
    resolve_classifier_incident(db_session, resolved, now=now)
    db_session.flush()

    context = _render_context(db_session, monkeypatch)
    ids = {str(r["incident"].id) for r in context["classifier_incidents"]}
    assert str(live.id) in ids
    assert str(discarded.id) not in ids
    assert str(resolved.id) not in ids


def test_detected_outages_route_registered():
    from app.web.admin.network_monitoring import router

    paths = {r.path for r in router.routes if hasattr(r, "methods")}
    assert "/network/detected-outages" in paths


def test_detected_outages_route_requires_monitoring_read():
    from app.web.admin.network_monitoring import router

    route = next(
        r
        for r in router.routes
        if getattr(r, "path", None) == "/network/detected-outages"
    )
    # The auth dependency is declared on the route (same pattern as siblings).
    assert route.dependencies, "route must carry a require_permission dependency"


def test_detected_outages_template_compiles():
    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/detected_outages.html"
    )
