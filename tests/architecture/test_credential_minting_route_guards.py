"""Credential-minting API routes carry their OWN admin guard, not just the mount.

This is defence in depth, NOT a vulnerability fix. Every minting path is already
admin-gated today and no privilege escalation was found: the wildcard scope is an
intentional, documented, tested capability for principals who are already admins
(``docs/designs/API_KEY_SCOPES.md``), and ``system:settings:write`` is
admin-only and non-assignable.

The fragility this pins is structural. Three ``app.api.auth`` routes mint or
rewrite API-key credentials, and their only authorization used to be the router
mount spec in ``app/main.py``::

    ("app.api.auth", "router", "api", "admin")

Every sibling route in that file (``user-credentials``, ``mfa-methods``) carries
its own ``require_permission``; these three carried nothing. Changing one word in
that tuple — ``"admin"`` -> ``"user"`` — would have opened API-key minting to any
authenticated principal, including a customer subscriber, with nothing to fall
back on.

So this module pins BOTH layers:

* layer 1 — the router-level admin mount is still declared in ``app/main.py``;
* layer 2 — each of the three routes declares its own ``require_role("admin")``.

Layer 2 is deliberately the admin ROLE rather than a permission key: the
authority to mint credentials must not silently widen if a permission later
becomes assignable to a non-admin role.

The route set is enumerated EXPLICITLY by method + path. That is the point — this
specific, high-value set is what must keep the second layer, so a refactor that
renames, moves or re-decorates one of these routes has to come back here.

``test_weakened_mount_is_still_denied_by_the_route_level_guard`` and
``test_without_the_route_level_guard_a_weakened_mount_lets_a_non_admin_through``
are the sensitivity pair: the first drives the exact regression this change
defends against (mount weakened to ``"user"``, non-admin principal) and proves it
is still refused; the second neutralizes only the route-level guard and proves
the same request then gets through — so the first assertion is known to be
testing the guard, not passing for some unrelated reason.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.auth import router as auth_router
from app.db import get_db
from app.main import _DEFERRED_API_ROUTER_SPECS, _mount_router
from app.models.rbac import Role
from app.services.auth_dependencies import require_user_auth

# The router-local (unprefixed) method + path of every route that mints a new
# API-key credential or rewrites an existing one's scopes. Mounted under
# ``/api/v1``. Do not generalize this into a pattern match: enumerating the set
# is what makes a rename or a move fail the build.
CREDENTIAL_MINTING_ROUTES = (
    ("POST", "/api-keys"),  # ApiKeys.create — mints a key from a caller-supplied hash
    ("POST", "/api-keys/generate"),  # ApiKeys.generate — mints a fresh secret
    ("PATCH", "/api-keys/{key_id}"),  # ApiKeys.update — the only scope-rewrite path
)

# The mount spec that is layer 1. Kept as an exact tuple so removing or
# weakening it fails here as well as in the route-level assertions below.
AUTH_ROUTER_MOUNT_SPEC = ("app.api.auth", "router", "api", "admin")

_REQUIRED_ROLE = "admin"

# A logged-in, non-admin staff principal: exactly who the router-level "user"
# mode would admit.
_NON_ADMIN_AUTH = {
    "principal_id": "00000000-0000-0000-0000-0000000000aa",
    "principal_type": "system_user",
    "roles": ["support"],
    "scopes": [],
}


class _FakeQuery:
    """Just enough of a Query to drive ``require_role`` deterministically.

    The admin Role row exists (so the guard does not short-circuit on
    "Role not found"), but the principal has no link to it — the guard must
    therefore refuse with a plain "Forbidden".
    """

    def __init__(self, model: object) -> None:
        self._model = model

    def filter(self, *args: object, **kwargs: object) -> _FakeQuery:
        return self

    def first(self) -> object | None:
        if self._model is Role:
            return SimpleNamespace(id="role-admin", name=_REQUIRED_ROLE, is_active=True)
        return None


class _FakeSession:
    def query(self, model: object) -> _FakeQuery:
        return _FakeQuery(model)


def _iter_dependants(dependant):
    yield dependant
    for sub in getattr(dependant, "dependencies", []) or []:
        yield from _iter_dependants(sub)


def _role_guards(dependant) -> set[str]:
    """Role names enforced by ``require_role`` guards under ``dependant``."""
    roles: set[str] = set()
    for dep in _iter_dependants(dependant):
        call = getattr(dep, "call", None)
        if getattr(call, "__name__", "") != "_require_role":
            continue
        for cell in call.__closure__ or ():
            value = cell.cell_contents
            if isinstance(value, str):
                roles.add(value)
    return roles


def _find_route(router, method: str, path: str) -> APIRoute:
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in (route.methods or set())
        ):
            return route
    raise AssertionError(
        f"{method} {path} is no longer a route on app.api.auth.router. "
        "If a credential-minting route was renamed or moved, update "
        "CREDENTIAL_MINTING_ROUTES here and keep its route-level admin guard."
    )


def _weakened_mount_app() -> FastAPI:
    """The production mount for ``app.api.auth`` with "admin" weakened to "user".

    This is the regression being defended against, reproduced exactly: same
    ``_mount_router`` machinery, same router, one word changed in the spec.
    """
    app = FastAPI()
    module_name, attr_name, mount_kind, _mode = AUTH_ROUTER_MOUNT_SPEC
    assert (module_name, attr_name, mount_kind) == ("app.api.auth", "router", "api")
    _mount_router(app, auth_router, mount_kind, "user")
    app.dependency_overrides[require_user_auth] = lambda: dict(_NON_ADMIN_AUTH)
    app.dependency_overrides[get_db] = _FakeSession
    return app


def test_the_auth_router_is_still_mounted_admin_only():
    """Layer 1: the route-level guards are belt-and-braces, not a replacement."""
    assert AUTH_ROUTER_MOUNT_SPEC in _DEFERRED_API_ROUTER_SPECS, (
        "app.api.auth must stay mounted with dependency mode 'admin'. The "
        "per-route guards added alongside it are a second layer, not a "
        "licence to relax the first."
    )


@pytest.mark.parametrize(("method", "path"), CREDENTIAL_MINTING_ROUTES)
def test_credential_minting_route_declares_its_own_admin_guard(method: str, path: str):
    """Layer 2: the guard is on the ROUTE, so it survives a mount-spec edit."""
    route = _find_route(auth_router, method, path)
    roles = _role_guards(route.dependant)
    assert _REQUIRED_ROLE in roles, (
        f"{method} {path} mints or rewrites an API-key credential and must "
        f'declare its own dependencies=[Depends(require_role("{_REQUIRED_ROLE}"))]. '
        "Inheriting authorization from the router mount spec alone means one "
        "edit in app/main.py opens credential minting to any authenticated "
        "principal. Use the admin ROLE, not a permission key: this authority "
        "must not widen if a permission later becomes assignable."
    )


@pytest.mark.parametrize(("method", "path"), CREDENTIAL_MINTING_ROUTES)
def test_weakened_mount_is_still_denied_by_the_route_level_guard(
    method: str, path: str
):
    """Sensitivity proof, forward direction.

    Mount weakened to "user" (the exact regression), authenticated non-admin
    principal: the route-level guard must still refuse.
    """
    client = TestClient(_weakened_mount_app(), raise_server_exceptions=False)
    url = "/api/v1" + path.replace("{key_id}", "00000000-0000-0000-0000-00000000000b")
    response = client.request(method, url, json={})
    assert response.status_code == 403, (
        f"{method} {path} was not refused for a non-admin once the router mount "
        f"was weakened to 'user' (got {response.status_code}). The route-level "
        "admin guard is the only thing standing between that mount-spec typo "
        "and credential minting by any authenticated principal."
    )
    assert response.json()["detail"] == "Forbidden"


@pytest.mark.parametrize(("method", "path"), CREDENTIAL_MINTING_ROUTES)
def test_without_the_route_level_guard_a_weakened_mount_lets_a_non_admin_through(
    method: str, path: str
):
    """Sensitivity proof, inverse direction — the canary is shown to bite.

    Same weakened mount, same non-admin principal, but the route-level
    ``require_role("admin")`` is neutralized (stand-in for it never having been
    added). The request must now get PAST authorization. The eventual adapter
    outcome is deliberately not pinned: depending on the route it may be
    schema validation, unavailable Redis, or the deliberately skeletal fake
    database. The contract under test is only that authorization no longer
    returns 403.
    """
    app = _weakened_mount_app()
    route = _find_route(auth_router, method, path)
    neutralized = 0
    for dep in _iter_dependants(route.dependant):
        call = getattr(dep, "call", None)
        if getattr(call, "__name__", "") == "_require_role":
            app.dependency_overrides[call] = lambda: dict(_NON_ADMIN_AUTH)
            neutralized += 1
    assert neutralized, (
        f"{method} {path} has no route-level require_role guard to neutralize, "
        "so the forward-direction proof above cannot be attributed to it."
    )

    client = TestClient(app, raise_server_exceptions=False)
    url = "/api/v1" + path.replace("{key_id}", "00000000-0000-0000-0000-00000000000b")
    response = client.request(method, url, json={})
    assert response.status_code != 403, (
        "Removing the route-level admin guard did NOT change the outcome, so "
        "the forward-direction assertion is passing for some other reason and "
        "this canary does not actually test the guard."
    )
