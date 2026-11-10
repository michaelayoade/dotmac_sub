from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from jinja2 import nodes

from app.web.admin import build_router
from app.web.admin import customer_retention as retention


def test_customer_retention_routes_are_registered_and_visible_from_hub():
    router = build_router()
    paths = {
        (getattr(route, "path", ""), frozenset(getattr(route, "methods", set())))
        for route in router.routes
    }

    assert ("/admin/customer-retention", frozenset({"GET"})) in paths
    assert ("/admin/customer-retention/metrics", frozenset({"GET"})) in paths
    assert ("/admin/customer-retention/queue", frozenset({"GET"})) in paths
    assert ("/admin/customer-retention/{customer_id}", frozenset({"GET"})) in paths

    from app.web.admin import reports

    links = [
        link for section in reports.REPORT_HUB_SECTIONS for link in section["links"]
    ]
    assert {
        "name": "Customer Retention",
        "url": "/admin/customer-retention",
        "description": "Billing-risk accounts prioritized for customer recovery",
        "permission": "reports:billing:read",
    } in links


def test_retention_templates_only_import_published_ui_macros():
    environment = retention.templates.env
    macro_module = environment.get_template("components/ui/macros.html").module

    for template_name in (
        "admin/reports/customer_retention_tracker.html",
        "admin/reports/_customer_retention_queue.html",
        "admin/reports/_customer_retention_metrics.html",
        "admin/reports/customer_retention_profile.html",
    ):
        source, _, _ = environment.loader.get_source(environment, template_name)
        parsed = environment.parse(source)
        imported_names = {
            imported if isinstance(imported, str) else imported[0]
            for node in parsed.find_all(nodes.FromImport)
            if getattr(node.template, "value", None) == "components/ui/macros.html"
            for imported in node.names
        }

        missing = sorted(
            name for name in imported_names if not hasattr(macro_module, name)
        )
        assert missing == [], f"{template_name} imports unavailable macros: {missing}"


def test_retention_tracker_exposes_bottom_right_pagination_controls():
    source, _, _ = retention.templates.env.loader.get_source(
        retention.templates.env, "admin/reports/_customer_retention_queue.html"
    )

    assert 'aria-label="Retention queue pages"' in source
    assert "Page {{ page }} of {{ total_pages }}" in source
    assert "page={{ page + 1 }}" in source
    assert "page={{ page - 1 }}" in source
    assert "View profile" in source


def test_retention_shell_loads_metrics_and_queue_independently():
    source, _, _ = retention.templates.env.loader.get_source(
        retention.templates.env, "admin/reports/customer_retention_tracker.html"
    )
    assert 'hx-get="{{ metrics_url }}"' in source
    assert 'hx-get="{{ queue_url }}"' in source
    assert 'name="date_from"' in source
    assert 'name="date_to"' in source


def test_retention_period_requires_both_ordered_dates():
    with pytest.raises(HTTPException):
        retention._period(date(2026, 11, 1), None)
    with pytest.raises(HTTPException):
        retention._period(date(2026, 11, 9), date(2026, 11, 1))
    assert retention._period(date(2026, 11, 1), date(2026, 11, 9)) == (
        date(2026, 11, 1),
        date(2026, 11, 9),
    )
