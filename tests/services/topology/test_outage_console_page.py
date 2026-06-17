"""Outage console route + template wiring (Phase 4b, P4.4)."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates


def test_console_routes_registered():
    from app.web.admin.network_monitoring import router

    paths = {
        (tuple(sorted(r.methods)), r.path)
        for r in router.routes
        if hasattr(r, "methods")
    }
    flat = {p for _, p in paths}
    assert "/network/outages" in flat
    assert "/network/outages/declare" in flat
    assert "/network/outages/{incident_id}/resolve" in flat


def test_console_template_compiles():
    Jinja2Templates(directory="templates").env.get_template(
        "admin/network/outages.html"
    )
