"""Outage console route + template wiring (Phase 4b, P4.4)."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services.topology.reachability import ReachabilityPage, ReachabilityPageQuery


def test_console_routes_registered():
    from app.web.admin.network_monitoring import router

    paths = {
        (tuple(sorted(r.methods)), r.path)
        for r in router.routes
        if hasattr(r, "methods")
    }
    flat = {p for _, p in paths}
    assert "/network/outages" in flat
    assert "/network/outages/reachability" in flat
    assert "/network/outages/declare" in flat
    assert "/network/outages/{incident_id}/resolve" in flat
    assert "/network/outages/{incident_id}/infrastructure-ticket" in flat


def test_console_template_compiles():
    template = Jinja2Templates(directory="templates").env.get_template(
        "admin/network/outages.html"
    )
    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/_outage_reachability.html"
    )
    source = Path("templates/admin/network/outages.html").read_text()
    assert "Create infrastructure ticket" in source
    assert 'include "admin/network/_outage_reachability.html"' in source
    assert 'scope="col">Actions</th>' in source
    assert 'colspan="5"' in source
    assert "Delivery, note &amp; related work" in source
    assert "Create field work" in source
    assert "Resolve" in source


def test_reachability_route_renders_only_its_table(db_session, monkeypatch):
    from app.web.admin import network_monitoring

    seen: list[ReachabilityPageQuery] = []

    def page_query(
        session: Session, *, query: ReachabilityPageQuery
    ) -> ReachabilityPage:
        assert session is db_session
        seen.append(query)
        return ReachabilityPage(rows=(), page=1, per_page=20, total=0, total_pages=1)

    monkeypatch.setattr(network_monitoring, "get_reachability_page", page_query)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/network/outages/reachability",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    response = network_monitoring.outage_reachability_page(
        request=request, reachability_page=1, db=db_session
    )

    assert seen == [ReachabilityPageQuery(page=1)]
    assert 'id="outage-reachability-page"' in response.body.decode()
    assert "Open outages" not in response.body.decode()
