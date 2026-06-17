"""Outage-impact admin page wiring (Phase 4a, P4.2)."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates


def test_route_registered():
    from app.web.admin.network_monitoring import router

    paths = {r.path for r in router.routes}
    assert "/network/outage-impact" in paths


def test_templates_compile():
    env = Jinja2Templates(directory="templates").env
    env.get_template("admin/network/outage_impact.html")
    env.get_template("admin/network/index.html")  # nav card added
