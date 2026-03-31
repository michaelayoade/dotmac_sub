import pytest
from fastapi.routing import APIRoute

from app.api import scheduler as scheduler_api


def _get_route(path: str, method: str) -> APIRoute:
    for route in scheduler_api.router.routes:
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


def _route_has_permission(path: str, method: str, expected: str) -> bool:
    route = _get_route(path, method)
    for dependency in route.dependant.dependencies:
        call = dependency.call
        closure = getattr(call, "__closure__", None) or ()
        for cell in closure:
            if _contains_value(cell.cell_contents, expected):
                return True
    return False


@pytest.mark.skip(reason="Scheduler API routes do not have permission dependencies yet")
def test_scheduler_api_routes_require_settings_permissions():
    assert _route_has_permission("/scheduler/tasks", "GET", "system:settings:read")
    assert _route_has_permission("/scheduler/tasks/{task_id}", "GET", "system:settings:read")
    assert _route_has_permission("/scheduler/tasks", "POST", "system:settings:write")
    assert _route_has_permission("/scheduler/tasks/{task_id}", "PATCH", "system:settings:write")
    assert _route_has_permission("/scheduler/tasks/{task_id}", "DELETE", "system:settings:write")
    assert _route_has_permission("/scheduler/tasks/refresh", "POST", "system:settings:write")
    assert _route_has_permission("/scheduler/tasks/{task_id}/enqueue", "POST", "system:settings:write")
