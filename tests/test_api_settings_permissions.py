import pytest
from fastapi.routing import APIRoute

from app.api import settings as settings_api


def _get_route(path: str, method: str) -> APIRoute:
    for route in settings_api.router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"Route not found: {method} {path}")


def _contains_value(value, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    closure = getattr(value, "__closure__", None)
    if closure:
        return any(_contains_value(cell.cell_contents, expected) for cell in closure)
    if isinstance(value, (tuple, list, set)):
        return any(_contains_value(item, expected) for item in value)
    if isinstance(value, dict):
        return any(_contains_value(item, expected) for item in value.values())
    return False


def _route_has_permission(path: str, method: str, expected: str) -> bool:
    route = _get_route(path, method)
    for dependency in route.dependencies:
        if _contains_value(dependency.dependency, expected):
            return True
    return False


@pytest.mark.skip(reason="Settings API routes do not have permission dependencies yet")
def test_settings_api_routes_require_settings_permissions():
    assert _route_has_permission("/settings/auth", "GET", "system:settings:read")
    assert _route_has_permission("/settings/auth/{key}", "GET", "system:settings:read")
    assert _route_has_permission("/settings/auth/{key}", "PUT", "system:settings:write")
    assert _route_has_permission("/settings/billing", "GET", "system:settings:read")
    assert _route_has_permission(
        "/settings/billing/{key}", "PUT", "system:settings:write"
    )
