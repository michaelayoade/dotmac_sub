"""Route registration + permission-guard tests for the native projects API
(Phase 3 §2.4)."""

from pathlib import Path

from app.api.projects import router
from app.services.auth_dependencies import permission_requirement

EXPECTED_ROUTES = {
    ("GET", "/projects/{project_id}/accepted-field-completion"),
    ("POST", "/projects/{project_id}/accepted-field-completion"),
    ("GET", "/projects/{project_id}/tasks/{task_id}/accepted-field-completion"),
    ("POST", "/projects/{project_id}/tasks/{task_id}/accepted-field-completion"),
    ("POST", "/projects"),
    ("GET", "/projects"),
    ("PATCH", "/projects/{project_id}"),
    ("DELETE", "/projects/{project_id}"),
    ("GET", "/projects/charts/summary"),
    ("GET", "/projects/kanban"),
    ("GET", "/projects/gantt"),
    ("POST", "/projects/gantt/due-date"),
    ("POST", "/projects/kanban/move"),
    ("GET", "/projects/{project_id}"),
    ("GET", "/projects/{project_id}/cost-summary"),
    ("POST", "/project-tasks"),
    ("GET", "/project-tasks"),
    ("GET", "/project-tasks/{task_id}"),
    ("PATCH", "/project-tasks/{task_id}"),
    ("POST", "/project-tasks/{task_id}/transition"),
    ("PUT", "/project-tasks/{task_id}/dependencies"),
    ("DELETE", "/project-tasks/{task_id}"),
}

EXPECTED_PERMISSIONS = {
    ("GET", "/projects/{project_id}/accepted-field-completion"): "project:update",
    ("POST", "/projects/{project_id}/accepted-field-completion"): "project:update",
    ("GET", "/projects/{project_id}/tasks/{task_id}/accepted-field-completion"): (
        "project:task:write"
    ),
    ("POST", "/projects/{project_id}/tasks/{task_id}/accepted-field-completion"): (
        "project:task:write"
    ),
    ("POST", "/projects"): "project:create",
    ("GET", "/projects"): "project:read",
    ("PATCH", "/projects/{project_id}"): "project:update",
    ("DELETE", "/projects/{project_id}"): "project:delete",
    ("GET", "/projects/charts/summary"): "project:read",
    ("GET", "/projects/kanban"): "project:read",
    ("GET", "/projects/gantt"): "project:read",
    ("POST", "/projects/gantt/due-date"): "project:update",
    ("POST", "/projects/kanban/move"): "project:update",
    ("GET", "/projects/{project_id}"): "project:read",
    ("GET", "/projects/{project_id}/cost-summary"): "project:read",
    ("POST", "/project-tasks"): "project:task:write",
    ("GET", "/project-tasks"): "project:task:read",
    ("GET", "/project-tasks/{task_id}"): "project:task:read",
    ("PATCH", "/project-tasks/{task_id}"): "project:task:write",
    ("POST", "/project-tasks/{task_id}/transition"): "project:task:write",
    ("PUT", "/project-tasks/{task_id}/dependencies"): "project:task:write",
    ("DELETE", "/project-tasks/{task_id}"): "project:task:write",
}


def _routes():
    for route in router.routes:
        for method in route.methods - {"HEAD", "OPTIONS"}:
            yield (method, route.path), route


def test_all_expected_routes_registered():
    assert {key for key, _route in _routes()} == EXPECTED_ROUTES


def _permission_keys(route, method: str) -> set[str]:
    """Inspect decorator and endpoint-parameter permission dependencies."""
    keys: set[str] = set()
    for dep in route.dependant.dependencies:
        requirement = permission_requirement(dep.call)
        if requirement is not None:
            keys.update(requirement.any_of_for(method))
    return keys


def test_every_route_has_expected_permission_guard():
    for key, route in _routes():
        expected = EXPECTED_PERMISSIONS[key]
        assert {expected} == _permission_keys(route, key[0]), (
            f"{key} missing permission guard {expected!r}"
        )


def test_router_registered_in_main_spec_table():
    main_py = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert '("app.api.projects", "router", "api", "user")' in main_py


def test_permission_keys_are_seeded():
    seed = (
        Path(__file__).resolve().parents[1] / "scripts" / "seed" / "seed_rbac.py"
    ).read_text()
    for permission in set(EXPECTED_PERMISSIONS.values()):
        assert f'"{permission}"' in seed, f"{permission} not seeded in RBAC"
