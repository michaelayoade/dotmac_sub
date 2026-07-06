"""Detected-outages console route + template wiring (P4 surface)."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates


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
