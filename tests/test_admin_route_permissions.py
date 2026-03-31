from fastapi.routing import APIRoute

from app.web.admin import admin_hub as admin_system_hub
from app.web.admin import catalog as admin_catalog
from app.web.admin import catalog_settings as admin_catalog_settings
from app.web.admin import configuration as admin_configuration
from app.web.admin import dashboard as admin_dashboard
from app.web.admin import design_system as admin_design_system
from app.web.admin import gis as admin_gis
from app.web.admin import integrations as admin_integrations
from app.web.admin import legal as admin_legal
from app.web.admin import reports as admin_reports
from app.web.admin import resellers as admin_resellers
from app.web.admin import usage as admin_usage


def _get_route(module_router, path: str, method: str) -> APIRoute:
    for route in module_router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def _contains_value(value, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, (tuple, list, set)):
        return any(_contains_value(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    return False


def _route_has_permission(module_router, path: str, method: str, expected: str) -> bool:
    route = _get_route(module_router, path, method)
    for dependency in route.dependant.dependencies:
        call = dependency.call
        closure = getattr(call, "__closure__", None) or ()
        for cell in closure:
            if _contains_value(cell.cell_contents, expected):
                return True
    return False


def test_catalog_routes_require_catalog_permissions():
    assert _route_has_permission(admin_catalog.router, "/catalog", "GET", "catalog:read")
    assert _route_has_permission(admin_catalog.router, "/catalog/offers", "POST", "catalog:write")


def test_dashboard_routes_require_any_domain_read_permission():
    assert _route_has_permission(
        admin_dashboard.router,
        "/dashboard",
        "GET",
        "billing:read",
    )


def test_catalog_settings_routes_require_catalog_permissions():
    assert _route_has_permission(
        admin_catalog_settings.router,
        "/catalog/settings",
        "GET",
        "catalog:read",
    )
    assert _route_has_permission(
        admin_catalog_settings.router,
        "/catalog/settings/region-zones",
        "POST",
        "catalog:write",
    )


def test_gis_routes_require_network_permissions():
    assert _route_has_permission(admin_gis.router, "/gis", "GET", "network:read")
    assert _route_has_permission(admin_gis.router, "/gis/locations/new", "POST", "network:write")
    assert _route_has_permission(
        admin_gis.router,
        "/gis/location-corrections/{request_id}/approve",
        "POST",
        "network:write",
    )


def test_reseller_routes_require_customer_permissions():
    assert _route_has_permission(admin_resellers.router, "/resellers", "GET", "customer:read")
    assert _route_has_permission(admin_resellers.router, "/resellers", "POST", "customer:write")


def test_design_system_routes_require_system_read():
    assert _route_has_permission(
        admin_design_system.router,
        "/design-system",
        "GET",
        "system:read",
    )


def test_system_hub_routes_require_system_read():
    assert _route_has_permission(
        admin_system_hub.router,
        "/admin-hub",
        "GET",
        "system:read",
    )
    assert _route_has_permission(
        admin_configuration.router,
        "/configuration",
        "GET",
        "system:read",
    )


def test_legal_routes_require_system_permissions():
    assert _route_has_permission(
        admin_legal.router,
        "/legal",
        "GET",
        "system:read",
    )
    assert _route_has_permission(
        admin_legal.router,
        "/legal/{document_id}/publish",
        "POST",
        "system:write",
    )


def test_integrations_routes_require_settings_permissions():
    assert _route_has_permission(
        admin_integrations.router,
        "/integrations/connectors",
        "GET",
        "system:settings:read",
    )
    assert _route_has_permission(
        admin_integrations.router,
        "/integrations/installed/{connector_id}/toggle",
        "POST",
        "system:settings:write",
    )
    assert _route_has_permission(
        admin_integrations.router,
        "/integrations/whatsapp/config",
        "POST",
        "system:settings:write",
    )


def test_usage_routes_require_catalog_permissions():
    assert _route_has_permission(
        admin_usage.router,
        "/catalog/usage",
        "GET",
        "catalog:read",
    )
    assert _route_has_permission(
        admin_usage.router,
        "/catalog/usage/charges/{charge_id}/post",
        "POST",
        "catalog:write",
    )
    assert _route_has_permission(
        admin_usage.router,
        "/catalog/usage/rating/run",
        "POST",
        "catalog:write",
    )


def test_report_routes_require_domain_permissions():
    assert _route_has_permission(admin_reports.router, "/reports/revenue", "GET", "billing:read")
    assert _route_has_permission(admin_reports.router, "/reports/subscribers", "GET", "customer:read")
    assert _route_has_permission(admin_reports.router, "/reports/network", "GET", "network:read")
    assert _route_has_permission(
        admin_reports.router,
        "/reports/technician",
        "GET",
        "provisioning:read",
    )
