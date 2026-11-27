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


def _render_context(db, monkeypatch, *, live=False, node_id=None, incident_page=1):
    from app.web.admin import network_monitoring

    monkeypatch.setattr(
        network_monitoring, "_base_context", lambda *a, **k: {"request": _request()}
    )
    monkeypatch.setattr(
        network_monitoring.templates,
        "TemplateResponse",
        lambda template, context: {"template": template, "context": context},
    )
    resp = network_monitoring.detected_outages_console(
        _request(),
        node_id=node_id,
        live=live,
        incident_page=incident_page,
        db=db,
    )
    return resp["context"]


def test_console_defers_live_sweep(db_session, monkeypatch):
    from app.web.admin import network_monitoring

    def unexpected_sweep(db):
        raise AssertionError("initial page must not run the live sweep")

    monkeypatch.setattr(
        network_monitoring, "_detected_outages_live_context", unexpected_sweep
    )
    context = _render_context(db_session, monkeypatch)
    assert context["show_live"] is False
    assert "summary" not in context
    assert "active" not in context


def test_console_defers_selected_domain_diagnosis(db_session, monkeypatch):
    from app.services.topology import outage_console

    def unexpected_detail(*args, **kwargs):
        raise AssertionError("initial page must not diagnose customers")

    monkeypatch.setattr(outage_console, "outage_detail", unexpected_detail)
    selected_id = uuid.uuid4()
    context = _render_context(db_session, monkeypatch, node_id=selected_id)
    assert context["selected_node_id"] == selected_id
    assert "detail" not in context


def test_console_paginates_incident_list(db_session, monkeypatch):
    from app.web.admin import network_monitoring

    calls = []

    def incidents(db, *, limit, offset):
        calls.append((limit, offset))
        return []

    monkeypatch.setattr(
        "app.services.topology.outage.list_classifier_incidents", incidents
    )
    context = _render_context(db_session, monkeypatch, incident_page=3)
    assert calls == [(network_monitoring.DETECTED_OUTAGES_PAGE_SIZE + 1, 100)]
    assert context["incident_page"] == 3
    assert context["incident_has_next"] is False


def test_console_full_page_live_fallback(db_session, monkeypatch):
    from app.web.admin import network_monitoring

    live_context = {"summary": {"counts": {}}, "active": {"outages": []}}
    monkeypatch.setattr(
        network_monitoring, "_detected_outages_live_context", lambda db: live_context
    )
    context = _render_context(db_session, monkeypatch, live=True)
    assert context["show_live"] is True
    assert context["summary"] is live_context["summary"]


def test_live_fragment_reuses_one_summary(db_session, monkeypatch):
    from app.services.topology import outage_console
    from app.web.admin import network_monitoring

    summary = {"counts": {}, "total_nodes": 0, "not_healthy": []}
    calls = []
    monkeypatch.setattr(outage_console, "network_health_summary", lambda db: summary)

    def active(db, *, summary):
        calls.append(summary)
        return {"outages": [], "monitoring_faults": []}

    monkeypatch.setattr(outage_console, "active_outages", active)
    monkeypatch.setattr(
        network_monitoring.templates,
        "TemplateResponse",
        lambda template, context: {"template": template, "context": context},
    )
    response = network_monitoring.detected_outages_live(_request(), db=db_session)
    assert response["template"] == "admin/network/_detected_outages_live.html"
    assert response["context"]["summary"] is summary
    assert calls == [summary]


def test_detail_fragment_only_loads_selected_domain(db_session, monkeypatch):
    from app.services.topology import outage_console
    from app.web.admin import network_monitoring

    selected_id = uuid.uuid4()
    calls = []

    def detail(db, node_id, **kwargs):
        calls.append((node_id, kwargs))
        return {"node": {"name": "Selected"}}

    monkeypatch.setattr(outage_console, "outage_detail", detail)
    monkeypatch.setattr(
        network_monitoring.templates,
        "TemplateResponse",
        lambda template, context: {"template": template, "context": context},
    )
    response = network_monitoring.detected_outages_detail(
        _request("/admin/network/detected-outages/detail"),
        node_id=selected_id,
        customer_page=2,
        db=db_session,
    )
    assert response["template"] == "admin/network/_detected_outages_detail.html"
    assert response["context"]["detail"]["node"]["name"] == "Selected"
    assert calls == [
        (
            selected_id,
            {
                "customer_offset": 50,
                "customer_limit": 50,
                "include_predictive": False,
            },
        )
    ]


def test_connectivity_fragment_does_not_read_tickets_without_permission(
    db_session, monkeypatch
):
    from app.services.topology import customer_connectivity as projection
    from app.web.admin import network_monitoring

    selected_id = uuid.uuid4()
    calls = []
    monkeypatch.setattr(network_monitoring, "can", lambda *args: False)
    monkeypatch.setattr(projection, "search_scopes", lambda *args, **kwargs: ())

    def detail(db, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(projection, "customer_connectivity", detail)
    monkeypatch.setattr(
        network_monitoring.templates,
        "TemplateResponse",
        lambda template, context: {"template": template, "context": context},
    )
    response = network_monitoring.detected_outages_connectivity(
        _request("/admin/network/detected-outages/connectivity"),
        scope="node",
        q="",
        scope_id=selected_id,
        status_filter="no_ticket",
        customer_page=1,
        db=db_session,
    )
    assert response["template"] == "admin/network/_detected_outages_connectivity.html"
    assert response["context"]["can_view_tickets"] is False
    assert calls[0]["include_tickets"] is False
    assert calls[0]["status_filter"] == projection.ConnectivityFilter.all


def test_branch_fragment_loads_one_pon_page(db_session, monkeypatch):
    from app.services.topology import outage_console
    from app.web.admin import network_monitoring

    selected_id = uuid.uuid4()
    calls = []

    def branches(db, node_id, *, page):
        calls.append((node_id, page))
        return {"co_failure": [], "rx_droop": [], "total_ports": 2}

    monkeypatch.setattr(outage_console, "predictive_branches_page", branches)
    monkeypatch.setattr(
        network_monitoring.templates,
        "TemplateResponse",
        lambda template, context: {"template": template, "context": context},
    )
    response = network_monitoring.detected_outages_branches(
        _request("/admin/network/detected-outages/branches"),
        node_id=selected_id,
        pon_page=2,
        db=db_session,
    )
    assert response["template"] == "admin/network/_detected_outages_branches.html"
    assert calls == [(selected_id, 2)]


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
    assert row["status_presentation"].model_dump(mode="json") == {
        "value": "confirmed",
        "label": "Confirmed",
        "tone": "negative",
        "icon": "alert",
    }
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
    live_route = next(
        r
        for r in router.routes
        if getattr(r, "path", None) == "/network/detected-outages/live"
    )
    assert live_route.dependencies
    detail_route = next(
        r
        for r in router.routes
        if getattr(r, "path", None) == "/network/detected-outages/detail"
    )
    assert detail_route.dependencies
    branches_route = next(
        r
        for r in router.routes
        if getattr(r, "path", None) == "/network/detected-outages/branches"
    )
    assert branches_route.dependencies


def test_detected_outages_template_compiles():
    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/detected_outages.html"
    )
    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/_detected_outages_live.html"
    )
    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/_detected_outages_detail.html"
    )
    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/_detected_outages_branches.html"
    )
