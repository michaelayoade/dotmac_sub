from __future__ import annotations

from pathlib import Path

from app.services import custom_field_capabilities

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


TARGETS = {
    "subscriber": "/admin/customers/person/{target_id}",
    "project": "/admin/projects/{target_id}",
    "support_ticket": "/admin/support/tickets/{target_id}",
    "work_order": "/admin/dispatch/work-orders/{target_id}",
    "lead": "/admin/sales/leads/{target_id}",
    "quote": "/admin/sales/quotes/{target_id}",
    "sales_order": "/admin/sales/sales-order/{target_id}",
}

SURFACES = (
    (
        "subscriber",
        "app/web/admin/customers.py",
        "templates/admin/customers/detail.html",
    ),
    (
        "project",
        "app/web/admin/projects.py",
        "templates/admin/projects/project_detail.html",
    ),
    (
        "support_ticket",
        "app/web/admin/support_tickets.py",
        "templates/admin/support/tickets/detail.html",
    ),
    (
        "work_order",
        "app/web/admin/dispatch_work_orders.py",
        "templates/admin/dispatch/work_order_detail.html",
    ),
    ("lead", "app/web/admin/sales.py", "templates/admin/sales/leads/detail.html"),
    ("quote", "app/web/admin/sales.py", "templates/admin/sales/quotes/detail.html"),
    (
        "sales_order",
        "app/web/admin/sales.py",
        "templates/admin/sales/sales_orders/detail.html",
    ),
)


def test_registered_targets_have_native_detail_surfaces() -> None:
    for target_type, detail_path in TARGETS.items():
        target = custom_field_capabilities.target_capability(target_type)
        assert target.detail_path_template == detail_path

    for target_type, route_path, template_path in SURFACES:
        route = _source(route_path)
        detail = _source(template_path)
        assert f'target_type="{target_type}"' in route
        assert 'include "admin/customers/_custom_fields.html"' in detail


def test_value_surfaces_preserve_custom_and_native_permissions() -> None:
    projection = _source("app/services/web_custom_fields.py")
    value_route = _source("app/web/admin/custom_fields.py")
    api = _source("app/api/custom_fields.py")
    partial = _source("templates/admin/customers/_custom_fields.html")

    assert "custom_fields.VALUE_READ_PERMISSION" in projection
    assert "target.read_permission" in projection
    assert "custom_fields.VALUE_WRITE_PERMISSION" in projection
    assert "target.write_permission" in projection
    assert "show_in_detail or row.definition.show_in_form" in projection
    assert "custom_field_access.target_access_allowed(" in projection
    assert "require_permission(custom_fields.VALUE_WRITE_PERMISSION)" in value_route
    assert "custom_field_access.target_access_allowed(" in value_route
    assert "custom_field_access.target_access_allowed(" in api
    assert "custom_fields.set_value(" in value_route
    assert "can_write_sensitive_custom_fields" in partial
    assert "field.show_in_form" in partial
    assert "custom_field_target_type" in partial
    assert "custom_field_target_id" in partial
    assert "custom_fields:sensitive:read" not in partial


def test_work_order_scoped_grants_are_checked_against_the_exact_record() -> None:
    access = _source("app/services/custom_field_access.py")
    assert 'target.key != "work_order"' in access
    assert "WorkOrder.id == target_id" in access
    assert "WorkOrder.public_id" in _source("app/services/custom_field_targets.py")
    assert "custom_field_targets.target_detail_path(" in _source(
        "app/web/admin/custom_fields.py"
    )
    assert '("reseller", str(subscriber.reseller_id))' in access
    assert '("region", subscriber.region)' in access
    assert "candidates.intersection(decision)" in access
